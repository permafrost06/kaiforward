#!/usr/bin/env python3
"""Read KaiForward's aggregate status through its debugger console."""
import json
from rdp import Client


def checked(response):
    if 'error' in response:
        raise RuntimeError(json.dumps(response))
    return response


def main():
    client = Client()
    try:
        actor = checked(client.request('root', 'listTabs'))['webappsActor']
        app = checked(client.request(actor, 'getAppActor', manifestURL='app://kaiforward.local/manifest.webapp'))['actor']
        checked(client.request(app['actor'], 'attach'))
        result = checked(client.request(app['consoleActor'], 'evaluateJS', text='JSON.stringify(window.KaiForward.stats)'))
        value = result.get('result')
        if isinstance(value, str):
            print(json.dumps(json.loads(value), indent=2))
        elif isinstance(value, dict) and 'value' in value:
            print(json.dumps(json.loads(value['value']), indent=2))
        elif isinstance(value, dict) and value.get('type') == 'longString':
            print('Status returned a long string; use the phone display.')
        else:
            print('No aggregate status available:', json.dumps(value))
    finally:
        client.close()


if __name__ == '__main__':
    main()
