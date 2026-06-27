"""Copy media objects from the old `stores-storage` bucket into `sops-storage`.

The seed media re-uploads itself into sops-storage on first app boot, so this
script is only needed to bring over *already-uploaded* (non-seed) objects.

It remaps the key prefix `stores/media/` -> `sops/media/` so the keys line up
with what this app expects (see media/storage.py: MEDIA_PREFIX).

Both buckets live on the same OVH S3 endpoint with the same credentials, so the
copy is download+upload through this machine.

Env (same as the app, plus the source bucket):
    OVH__ENDPOINT_URL, OVH__REGION, OVH__ACCESS_KEY, OVH__SECRET_KEY
    OVH__BUCKET            destination bucket (default: sops-storage)
    SRC_BUCKET             source bucket      (default: stores-storage)
    SRC_PREFIX             source prefix      (default: stores/media/)
    DST_PREFIX             dest prefix        (default: sops/media/)

Usage:
    python tools/migrate_s3.py            # copy everything under SRC_PREFIX
    python tools/migrate_s3.py --dry-run  # list what would be copied
"""
import argparse
import os
import sys

import boto3
from botocore.client import Config as BotoConfig


def client():
    return boto3.client(
        's3',
        endpoint_url=os.environ['OVH__ENDPOINT_URL'],
        region_name=os.environ.get('OVH__REGION'),
        aws_access_key_id=os.environ['OVH__ACCESS_KEY'],
        aws_secret_access_key=os.environ['OVH__SECRET_KEY'],
        config=BotoConfig(signature_version='s3v4'),
    )


def main():
    ap = argparse.ArgumentParser(description='Migrate S3 media stores-storage -> sops-storage')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--src-bucket', default=os.environ.get('SRC_BUCKET', 'stores-storage'))
    ap.add_argument('--dst-bucket', default=os.environ.get('OVH__BUCKET', 'sops-storage'))
    ap.add_argument('--src-prefix', default=os.environ.get('SRC_PREFIX', 'stores/media/'))
    ap.add_argument('--dst-prefix', default=os.environ.get('DST_PREFIX', 'sops/media/'))
    args = ap.parse_args()

    s3 = client()
    paginator = s3.get_paginator('list_objects_v2')
    copied = skipped = 0
    for page in paginator.paginate(Bucket=args.src_bucket, Prefix=args.src_prefix):
        for obj in page.get('Contents', []):
            src_key = obj['Key']
            dst_key = args.dst_prefix + src_key[len(args.src_prefix):]
            # Skip if the destination already has it (idempotent re-runs).
            try:
                s3.head_object(Bucket=args.dst_bucket, Key=dst_key)
                skipped += 1
                continue
            except Exception:
                pass
            print(f'{"DRY " if args.dry_run else ""}copy {src_key} -> {dst_key}')
            if not args.dry_run:
                body = s3.get_object(Bucket=args.src_bucket, Key=src_key)
                s3.put_object(
                    Bucket=args.dst_bucket, Key=dst_key,
                    Body=body['Body'].read(),
                    ContentType=body.get('ContentType', 'application/octet-stream'),
                )
            copied += 1
    print(f'\nDone. {copied} copied, {skipped} already present.', file=sys.stderr)


if __name__ == '__main__':
    main()
