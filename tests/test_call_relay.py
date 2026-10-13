"""Call transport: authentication, framing, command certainty and audio ownership."""
import asyncio
from contextlib import suppress
import importlib.util
import json
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('call_relay', ROOT / 'server/call_relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class CallRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = relay.Relay({'device_id':'fixture-phone', 'token':'fixture-secret'})
        self.server = await asyncio.start_server(self.relay.phone_session, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.sleep(.01)

    async def phone(self, token='fixture-secret'):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.writers.append(writer)
        writer.write(relay.encode_frame(relay.HELLO, json.dumps({'device_id':'fixture-phone','token':token,'protocol':1}).encode()))
        await writer.drain()
        return reader, writer

    async def connected_phone(self):
        reader, writer = await self.phone()
        kind, payload = await asyncio.wait_for(relay.read_frame(reader), 1)
        self.assertEqual(kind, relay.HELLO)
        self.assertTrue(json.loads(payload)['ok'])
        return reader, writer

    async def test_frame_bounds_and_fragmented_audio(self):
        reader = asyncio.StreamReader()
        packet = relay.encode_frame(relay.DOWNLINK, b'\0'*320)
        task = asyncio.create_task(relay.read_frame(reader))
        for byte in packet:
            reader.feed_data(bytes([byte]))
            await asyncio.sleep(0)
        self.assertEqual(await task, (relay.DOWNLINK,b'\0'*320))
        for size in (0,8193,0xffffffff):
            reader = asyncio.StreamReader();reader.feed_data(struct.pack('>I',size))
            with self.assertRaises(ValueError):
                await relay.read_frame(reader)

    async def test_wrong_token_cannot_obtain_phone_session(self):
        reader, _ = await self.phone('wrong')
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertFalse(self.relay.public_state()['phone_connected'])

    async def test_second_connection_does_not_replace_authenticated_phone(self):
        _, first = await self.connected_phone()
        active = self.relay.phone
        reader, _ = await self.phone()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertIs(self.relay.phone, active)
        self.assertFalse(first.is_closing())

    async def test_call_result_follows_modem_reply_and_is_sent_once(self):
        reader, writer = await self.connected_phone()
        task = asyncio.create_task(self.relay.command({'slot':2,'action':'answer'}))
        kind, payload = await asyncio.wait_for(relay.read_frame(reader), 1)
        self.assertEqual(kind, relay.COMMAND)
        self.assertEqual(payload[4:], b'2 answer')
        self.assertFalse(task.done())
        writer.write(relay.encode_frame(relay.RESULT,payload[:4]+b'{"ok":true,"result":0}'))
        await writer.drain()
        self.assertTrue((await asyncio.wait_for(task, 1))['ok'])
        self.assertEqual(self.relay.pending,{})

    async def test_disconnect_reports_uncertain_without_replaying_after_reconnect(self):
        reader, writer = await self.connected_phone()
        task = asyncio.create_task(self.relay.command({'slot':2,'action':'hangup'}))
        kind, _ = await asyncio.wait_for(relay.read_frame(reader), 1)
        self.assertEqual(kind,relay.COMMAND)
        writer.close();await writer.wait_closed()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(task,1)
        self.assertEqual(self.relay.pending,{})
        new_reader,_ = await self.connected_phone()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(relay.read_frame(new_reader),.05)

    async def test_audio_starts_only_for_attached_browser_and_active_selected_sim(self):
        reader,writer = await self.connected_phone()
        report={'slots':[{'slot':1,'calls':0,'state':-1},{'slot':2,'calls':1,'state':0}], 'media':'idle'}
        writer.write(relay.encode_frame(relay.STATE,json.dumps(report).encode()));await writer.drain()
        self.assertEqual(await asyncio.wait_for(relay.read_frame(reader),1),(relay.PING,b''))
        class Browser:
            def __init__(self):self.received=[]
            async def send(self,data):self.received.append(data)
        browser=Browser();self.relay.audio_owner=browser;self.relay.audio_slot=2
        writer.write(relay.encode_frame(relay.STATE,json.dumps(report).encode()));await writer.drain()
        self.assertEqual(await asyncio.wait_for(relay.read_frame(reader),1),(relay.PING,b''))
        self.assertEqual(await asyncio.wait_for(relay.read_frame(reader),1),(relay.START,b'\x02'))
        writer.write(relay.encode_frame(relay.DOWNLINK,b'\1\0'*160));await writer.drain()
        for _ in range(20):
            if browser.received:break
            await asyncio.sleep(.01)
        self.assertEqual(browser.received,[b'\1\0'*160])
        await self.relay.detach_audio(browser)
        self.assertEqual(await asyncio.wait_for(relay.read_frame(reader),1),(relay.STOP,b''))

    async def test_commands_reject_ussd_injection_and_invalid_sim(self):
        self.assertEqual(relay.validate_command({'slot':2,'action':'dial','number':'+8801712345678'}), b'2 dial +8801712345678')
        for message in [{'slot':True,'action':'answer'},{'slot':3,'action':'answer'},
                        {'slot':2,'action':'dial','number':'*123#'},
                        {'slot':2,'action':'dial','number':'123\n2 answer'},
                        {'slot':2,'action':'dial','number':'১২৩'},{'slot':2,'action':'exec'}]:
            with self.assertRaises(ValueError):relay.validate_command(message)


if __name__ == '__main__':
    unittest.main()
