#!/usr/bin/env python3
import json
from pathlib import Path
from rdp import Client

APP_ID = 'kaiforward.local'
MANIFEST = 'app://' + APP_ID + '/manifest.webapp'


def checked(response):
    if 'error' in response:
        raise RuntimeError(json.dumps(response))
    return response


def main():
    package = Path(__file__).resolve().parents[1] / 'dist' / 'kaiforward.zip'
    client = Client()
    try:
        actor = checked(client.request('root', 'listTabs'))['webappsActor']
        # Updating an existing app must not leave its previous script running.
        existing = client.request(actor, 'getApp', manifestURL=MANIFEST)
        if existing.get('error') != 'appNotFound':
            checked(existing)
            checked(client.request(actor, 'close', manifestURL=MANIFEST))
        upload = checked(client.request(actor, 'uploadPackage', bulk=False))['actor']
        data = package.read_bytes()
        for offset in range(0, len(data), 16384):
            checked(client.request(upload, 'chunk', chunk=data[offset:offset + 16384].decode('latin1')))
        checked(client.request(upload, 'done'))
        result = checked(client.request(actor, 'install', appId=APP_ID, upload=upload))
        print('Installed:', result.get('appId', APP_ID))
        checked(client.request(actor, 'launch', manifestURL=MANIFEST))
        print('Launched KaiForward')
    finally:
        client.close()


if __name__ == '__main__':
    main()
