"""S3-compatible object storage for the media library (OVH Object Storage).

Config comes from the `ovh` section (see config/env_loader.py):
    OVH__ENDPOINT_URL, OVH__BUCKET, OVH__REGION, OVH__ACCESS_KEY, OVH__SECRET_KEY

All media bytes live in the bucket under the `MEDIA_PREFIX`; the database only
stores metadata (slug, key, content-type, size). The app proxies reads through
GET /media/file/<slug> so URLs stay stable and auth-gated.
"""
from flask import current_app

MEDIA_PREFIX = 'sops/media/'


def _cfg():
    return (current_app.config.get('ovh') or {})


def is_configured():
    c = _cfg()
    return bool(c.get('bucket') and c.get('access_key') and c.get('secret_key')
               and c.get('endpoint_url'))


def get_client():
    import boto3
    from botocore.client import Config as BotoConfig
    c = _cfg()
    return boto3.client(
        's3',
        endpoint_url=c.get('endpoint_url'),
        region_name=c.get('region'),
        aws_access_key_id=c.get('access_key'),
        aws_secret_access_key=c.get('secret_key'),
        config=BotoConfig(signature_version='s3v4'),
    )


def bucket():
    return _cfg().get('bucket')


def put_object(key, data, content_type='application/octet-stream'):
    get_client().put_object(Bucket=bucket(), Key=key, Body=data,
                            ContentType=content_type)
    return key


def object_exists(key):
    try:
        get_client().head_object(Bucket=bucket(), Key=key)
        return True
    except Exception:
        return False


def get_object_bytes(key):
    obj = get_client().get_object(Bucket=bucket(), Key=key)
    return obj['Body'].read(), obj.get('ContentType', 'application/octet-stream')


def delete_object(key):
    try:
        get_client().delete_object(Bucket=bucket(), Key=key)
    except Exception:
        pass
