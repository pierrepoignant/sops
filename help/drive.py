"""Importing Google Drive files into the SOP attachment library.

The browser does the picking: Google Identity Services hands the page a
short-lived ``drive.readonly`` access token, the Google Picker returns the ids
of the chosen files, and this module fetches their bytes so the attachment
routes can store them exactly like a file uploaded from a disk.

The access token is never persisted. It arrives with the request, is used for
that request's downloads and is dropped — there is no refresh token, no stored
credential, and nothing about Drive is kept beyond the copied file itself.

An import is a *copy*, taken at that moment: editing the document in Drive
afterwards does not change the attachment. That is deliberate on a quality
platform, where an approved SOP must not have its annexes mutate underneath it.

Configuration (per deployment, section ``google-drive``):
    api_key   browser API key, restricted to the brand hosts — the Picker's
              "developer key"
    app_id    the Google Cloud project *number* (optional; lets the Picker show
              files the app itself created)
The OAuth client id is the brand's existing Sign-in-with-Google client.
"""
import requests
from flask import current_app

DRIVE_API = 'https://www.googleapis.com/drive/v3'
SCOPE = 'https://www.googleapis.com/auth/drive.readonly'

# Guard rails: WeasyPrint and the pod's memory both suffer from very large
# attachments, and a mis-click in the Picker should not pull in a whole folder.
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES = 25
TIMEOUT = 30

# Google-native documents have no bytes to download — they are exported. Keyed
# by Drive mime type: (export mime type, extension).
EXPORT_FORMATS = {
    'application/vnd.google-apps.document': ('application/pdf', 'pdf'),
    'application/vnd.google-apps.presentation': ('application/pdf', 'pdf'),
    'application/vnd.google-apps.drawing': ('application/pdf', 'pdf'),
    'application/vnd.google-apps.spreadsheet': (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xlsx'),
}


class DriveError(Exception):
    """A single file could not be imported. The message is shown to the user,
    so it names the file and says what went wrong."""


def _config():
    return current_app.config.get('google-drive', {}) or {}


def picker_config(brand):
    """What the page needs to open the Picker, or None when Drive import is not
    configured for this deployment/brand."""
    from brands import provider_for_brand
    from brands import PROVIDERS
    cfg = _config()
    api_key = cfg.get('api_key')
    oauth = current_app.config.get(PROVIDERS.get(provider_for_brand(brand)), {}) or {}
    client_id = oauth.get('client_id')
    if not (api_key and client_id):
        return None
    return {'api_key': api_key, 'client_id': client_id,
            'app_id': cfg.get('app_id') or '', 'scope': SCOPE}


def _get(path, token, params=None, stream=False):
    resp = requests.get(f'{DRIVE_API}{path}',
                        headers={'Authorization': f'Bearer {token}'},
                        params=params or {}, timeout=TIMEOUT, stream=stream)
    if resp.status_code in (401, 403):
        raise DriveError('accès Google Drive refusé ou expiré — relancez '
                         'l\'import pour vous reconnecter')
    if resp.status_code == 404:
        raise DriveError('fichier introuvable dans Drive')
    if not resp.ok:
        raise DriveError(f'Google Drive a répondu {resp.status_code}')
    return resp


def _download(path, token, params):
    """Read a response body, refusing to buffer more than MAX_FILE_BYTES.
    Exported Google docs announce no size up front, so the cap is enforced here
    rather than from the metadata alone."""
    resp = _get(path, token, params, stream=True)
    chunks, total = [], 0
    for chunk in resp.iter_content(chunk_size=256 * 1024):
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise DriveError(f'fichier trop volumineux '
                             f'(limite {MAX_FILE_BYTES // (1024 * 1024)} Mo)')
        chunks.append(chunk)
    return b''.join(chunks)


def fetch(file_id, token):
    """(filename, content_type, bytes) for one Drive file id.

    Google-native documents are exported (Docs/Slides/Drawings to PDF, Sheets to
    xlsx) and get the matching extension appended; anything else is downloaded
    as-is. Raises DriveError with a user-facing message."""
    meta = _get(f'/files/{file_id}', token,
                {'fields': 'name,mimeType,size',
                 'supportsAllDrives': 'true'}).json()
    name = meta.get('name') or file_id
    mime = meta.get('mimeType') or ''
    try:
        size = int(meta.get('size') or 0)
    except (TypeError, ValueError):
        size = 0
    if size > MAX_FILE_BYTES:
        raise DriveError(f'{name} : fichier trop volumineux '
                         f'(limite {MAX_FILE_BYTES // (1024 * 1024)} Mo)')

    if mime.startswith('application/vnd.google-apps.'):
        export = EXPORT_FORMATS.get(mime)
        if not export:
            raise DriveError(f'{name} : ce type de fichier Google ne peut pas '
                             'être exporté (formulaire, carte…)')
        export_mime, ext = export
        data = _download(f'/files/{file_id}/export', token,
                         {'mimeType': export_mime})
        if not name.lower().endswith(f'.{ext}'):
            name = f'{name}.{ext}'
        return name, export_mime, data

    data = _download(f'/files/{file_id}', token,
                     {'alt': 'media', 'supportsAllDrives': 'true'})
    return name, mime or 'application/octet-stream', data
