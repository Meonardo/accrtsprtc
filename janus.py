import argparse
import asyncio
import logging
import platform
import random
import string
import time
import websockets
import json
import attr
import datetime

from urllib.parse import urlencode
from urllib.request import Request, urlopen
from websockets.exceptions import ConnectionClosed, ConnectionClosedError
from aiortc.contrib.media import MediaPlayer
from collections import OrderedDict
from h264track import FFmpegH264Track
from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.rtcrtpparameters import RTCRtpCodecCapability
from streamplayer import StreamPlayer
from typing import Optional


old_print = print


def timestamped_print(*args, **kwargs):
    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(
        sep=' ',
        timespec='milliseconds')
    old_print(time_str, *args, **kwargs)


print = timestamped_print


capabilities = RTCRtpSender.getCapabilities("video")
codec_parameters = OrderedDict(
    [
        ("packetization-mode", "1"),
        ("level-asymmetry-allowed", "1"),
        ("profile-level-id", "42e01f"),
    ]
)
h264_capability = RTCRtpCodecCapability(
    mimeType="video/H264", clockRate=90000, channels=None, parameters=codec_parameters
)
preferences = [h264_capability]
RATE = 30


@attr.s
class JanusEvent:
    sender = attr.ib(validator=attr.validators.instance_of(int))


@attr.s
class PluginData(JanusEvent):
    plugin = attr.ib(validator=attr.validators.instance_of(str))
    data = attr.ib()
    jsep = attr.ib()


@attr.s
class JanusError:
    code = attr.ib(validator=attr.validators.instance_of(int))
    reason = attr.ib(validator=attr.validators.instance_of(str))


@attr.s
class WebrtcUp(JanusEvent):
    pass


@attr.s
class Media(JanusEvent):
    receiving = attr.ib(validator=attr.validators.instance_of(bool))
    kind = attr.ib(validator=attr.validators.in_(["audio", "video"]))

    @kind.validator
    def validate_kind(self, attribute, kind):
        if kind not in ["video", "audio"]:
            raise ValueError("kind must equal video or audio")


@attr.s
class SlowLink(JanusEvent):
    uplink = attr.ib(validator=attr.validators.instance_of(bool))
    lost = attr.ib(validator=attr.validators.instance_of(int))


@attr.s
class HangUp(JanusEvent):
    reason = attr.ib(validator=attr.validators.instance_of(str))


@attr.s(cmp=False)
class Ack:
    transaction = attr.ib(validator=attr.validators.instance_of(str))


@attr.s
class Jsep:
    sdp = attr.ib()
    type = attr.ib(validator=attr.validators.in_(["offer", "pranswer", "answer", "rollback"]))


@attr.s
class JanusGateway:
    server = attr.ib(validator=attr.validators.instance_of(str))
    _messages = attr.ib(factory=set)

    async def connect(self):
        self.conn = await websockets.connect(self.server, subprotocols=['janus-protocol'])
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "create",
            "transaction": transaction
        }))
        resp = await self.conn.recv()
        print(resp)
        parsed = json.loads(resp)
        assert parsed["janus"] == "success", "Failed creating session"
        assert parsed["transaction"] == transaction, "Incorrect transaction"
        self.session = parsed["data"]["id"]

    async def close(self):
        await self.conn.close()

    async def leave(self):
        if self.conn.closed:
            return
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "destroy",
            "session_id": self.session,
            "transaction": transaction
        }))
        # resp = await self.conn.recv()
        # print ("left room: ", resp)

    async def attach(self, plugin):
        assert hasattr(self, "session"), "Must connect before attaching to plugin"
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "attach",
            "session_id": self.session,
            "plugin": plugin,
            "transaction": transaction
        }))
        resp = await self.conn.recv()
        parsed = json.loads(resp)
        assert parsed["janus"] == "success", "Failed attaching to {}".format(plugin)
        assert parsed["transaction"] == transaction, "Incorrect transaction"
        self.handle = parsed["data"]["id"]

    async def sendtrickle(self, candidate):
        assert hasattr(self, "session"), "Must connect before sending messages"
        assert hasattr(self, "handle"), "Must attach before sending messages"
        transaction = transaction_id()
        janus_message = {
            "janus": "trickle",
            "session_id": self.session,
            "handle_id": self.handle,
            "transaction": transaction,
            "candidate": candidate
        }
        await self.conn.send(json.dumps(janus_message))

    async def sendmessage(self, body, jsep=None):
        assert hasattr(self, "session"), "Must connect before sending messages"
        assert hasattr(self, "handle"), "Must attach before sending messages"
        transaction = transaction_id()
        janus_message = {
            "janus": "message",
            "session_id": self.session,
            "handle_id": self.handle,
            "transaction": transaction,
            "body": body
        }
        if jsep is not None:
            janus_message["jsep"] = jsep
        await self.conn.send(json.dumps(janus_message))

    async def keepalive(self):
        assert hasattr(self, "session"), "Must connect before sending messages"
        assert hasattr(self, "handle"), "Must attach before sending messages"

        while True:
            try:
                if self.conn.closed:
                    return
                await asyncio.sleep(60)
                transaction = transaction_id()
                await self.conn.send(json.dumps({
                    "janus": "keepalive",
                    "session_id": self.session,
                    "handle_id": self.handle,
                    "transaction": transaction
                }))
            except (KeyboardInterrupt, ConnectionClosed, ConnectionClosedError) as e:
                return

    async def recv(self):
        if len(self._messages) > 0:
            return self._messages.pop()
        else:
            return await self._recv_and_parse()

    async def _recv_and_parse(self):
        raw = json.loads(await self.conn.recv())
        print("Received: ", raw)
        janus = raw["janus"]

        if janus == "event":
            return PluginData(
                sender=raw["sender"],
                plugin=raw["plugindata"]["plugin"],
                data=raw["plugindata"]["data"],
                jsep=raw["jsep"] if "jsep" in raw else None
            )
        elif janus == "webrtcup":
            return WebrtcUp(
                sender=raw["sender"]
            )
        elif janus == "media":
            return Media(
                sender=raw["sender"],
                receiving=raw["receiving"],
                kind=raw["type"]
            )
        elif janus == "slowlink":
            return SlowLink(
                sender=raw["sender"],
                uplink=raw["uplink"],
                lost=raw["lost"]
            )
        elif janus == "hangup":
            return HangUp(
                sender=raw["sender"],
                reason=raw["reason"]
            )
        elif janus == "ack":
            return Ack(
                transaction=raw["transaction"]
            )
        elif janus == "error":
            return JanusError(
                code=raw["error"]["code"],
                reason=raw["error"]["reason"]
            )
        else:
            return raw


class WebRTCClient:
    def __init__(self, signaling: JanusGateway, rtsp, mic, publisher):
        self.signaling = signaling
        self.rtsp = rtsp
        self.mic = mic
        self.publisher = publisher
        self.pc: Optional[RTCPeerConnection] = None
        self.stream_player: Optional[StreamPlayer] = None
        self.turn = None
        self.turn_user = None
        self.turn_passwd = None
        self.stun = None

    async def destroy(self):
        await self.http_session.close()
        await self.signaling.leave()
        if self.pc is not None:
            await self.pc.close()
        if self.stream_player is not None:
            self.stream_player.stop()

    async def handle_plugin_data(self, data):
        print("handle plugin data: \n", data)

        if data.jsep is not None:
            await self.handle_sdp(data.jsep)
        if data.data is not None:
            events_type = data.data["videoroom"]
            if events_type == "joined":
                await self.publish()
                publishers = data.data["publishers"]
                print("Publishes in the room: ", publishers)
            elif events_type == "event":
                obj = json.dumps(data.data, indent=4)
                msg = obj.encode(encoding='utf_8')
                if 'error' in data.data and 'error_code' in data.data:
                    raise Exception(msg)
                if 'leaving' in data.data and 'reason' in data.data:
                    leaving = data.data['error']
                    reason = data.data['reason']
                    if leaving == 'ok' and reason == 'kicked':
                        raise Exception(msg)

    async def handle_sdp(self, msg):
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert (msg['type'] == 'answer')
            print('Received answer:\n%s' % sdp)

            # apply answer
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=msg['type'])
            )
            for t in self.pc.getTransceivers():
                if t.kind == "video":
                    t.setCodecPreferences(preferences)

    async def publish(self):
        ice_configs = []
        if self.turn is not None and \
                self.turn_user is not None \
                and self.turn_passwd is not None:
            ice_configs.append(RTCIceServer(self.turn, self.turn_user, self.turn_passwd))

        if self.stun is not None:
            ice_configs.append(RTCIceServer(self.stun))

        if len(ice_configs) > 0:
            pc = RTCPeerConnection(configuration=RTCConfiguration(ice_configs))
        else:
            pc = RTCPeerConnection()
        self.pc = pc

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            print("ICE connection state is", pc.iceConnectionState)
            send_msg_to_main('ice', pc.iceConnectionState, self.publisher)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            print("Connection state is", pc.connectionState)
            send_msg_to_main('pc', pc.connectionState, self.publisher)

        request = {"request": "configure", "audio": False, "video": True}
        # configure media
        if self.rtsp is not None:
            if self.mic != 'mute':
                print("Current mic is: ", self.mic)
                if platform.system() == "Darwin":
                    player = MediaPlayer(':0', format='avfoundation', options={
                        '-rtbufsize': "512M"
                    })
                elif platform.system() == "Linux":
                    player = MediaPlayer("hw:2", format="alsa")
                else:
                    if self.mic is None:
                        self.mic = "Microphone (High Definition Audio Device)"
                    input_a = "audio={}".format(self.mic)
                    player = MediaPlayer(input_a, format="dshow", options={
                        '-rtbufsize': "512M"
                    })

                if player.audio is not None:
                    request["audio"] = True
                    pc.addTrack(player.audio)
            if self.rtsp == 'screen':
                player = MediaPlayer('video=screen-capture-recorder', format="dshow", options={
                    '-framerate': '30', '-b:v': '4M', '-video_size': '1920x1080'
                })
                pc.addTrack(player.video)
            else:
                rtsp_player = StreamPlayer(self.rtsp)
                video_track = FFmpegH264Track(rtsp_player)
                # self.camera = GstH264Player(video_track, self.rtsp)
                pc.addTrack(video_track)
                self.stream_player = rtsp_player
        else:
            raise Exception("No Media Input! Stop Now.")

        # send offer
        await pc.setLocalDescription(await pc.createOffer())

        if len(ice_configs) > 0:
            sdp = {"sdp": pc.localDescription.sdp, "trickle": True, "type": pc.localDescription.type}
        else:
            sdp = {"sdp": pc.localDescription.sdp, "trickle": False, "type": pc.localDescription.type}

        await self.signaling.sendmessage(request, sdp)

    async def republish(self, pc):
        await pc.close()
        print("Republishing...")
        if self.stream_player is not None:
            self.stream_player.stop()
        time.sleep(3)
        await self.publish()

    async def loop(self, signaling, room, display):
        await signaling.connect()
        await signaling.attach("janus.plugin.videoroom")

        loop = asyncio.get_event_loop()
        loop.create_task(signaling.keepalive())

        message = {"request": "join", "ptype": "publisher", "room": int(room), "pin": str(room), "display": display,
                   "id": int(self.publisher)}
        await signaling.sendmessage(message)

        assert signaling.conn

        while True:
            try:
                msg = await signaling.recv()
                if isinstance(msg, PluginData):
                    await self.handle_plugin_data(msg)
                elif isinstance(msg, Media):
                    print(msg)
                elif isinstance(msg, JanusError):
                    send_msg_to_main('error', msg.code, self.publisher)
                    print(msg)
                elif isinstance(msg, WebrtcUp):
                    send_msg_to_main('webrtc', 'up', self.publisher)
                    print(msg)
                elif isinstance(msg, SlowLink):
                    print(msg)
                elif isinstance(msg, HangUp):
                    print(msg)
                elif not isinstance(msg, Ack):
                    print(msg)
            except (KeyboardInterrupt, ConnectionClosed, ConnectionClosedError) as e:
                print("---------- Websocket exception: ", e)
                return


def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))


def send_msg_to_main(type, data, publisher):
    url = 'http://127.0.0.1:9001/camera/subprocess'
    msg = {'event': type, 'data': data, 'id': publisher}
    try:
        request = Request(url, urlencode(msg).encode())
        r = urlopen(request).read().decode()
        print("main process response:", r)
    except Exception as e:
        print("Send msg to main process exception", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Janus")
    parser.add_argument("url", help="Janus root URL, e.g. ws://localhost:8188")
    parser.add_argument("--rtsp", help="RTSP stream address.")
    parser.add_argument("--room", default="1234", help="The video room ID to join (default: 1234).", )
    parser.add_argument("--name", default="LocalCamera", help="The name display in the room", )
    parser.add_argument("--id", help="The ID of the camera in the videoroom(publishId)", )
    parser.add_argument("--mic", help="Specific a microphone device to record audio.")
    parser.add_argument("--turn", help="WebRTC turn server")
    parser.add_argument("--turn_user", help="WebRTC turn server username")
    parser.add_argument("--turn_passwd", help="WebRTC turn server passwd")
    parser.add_argument("--stun", help="WebRTC stun server")
    parser.add_argument("--log_level", "-L", default=0, help="Log level")
    args = parser.parse_args()
    print("Received Params:", args)

    if args.log_level:
        log_level = int(args.log_level)
        if log_level == 1:
            logging.basicConfig(level=logging.WARN)
        elif log_level == 2:
            logging.basicConfig(level=logging.INFO)
        elif log_level == 3:
            logging.basicConfig(level=logging.DEBUG)
        else:
            logging.basicConfig(level=logging.ERROR)
    rtsp = args.rtsp
    # create signaling client
    signaling = JanusGateway(args.url)
    # create webrtc client
    rtc_client = WebRTCClient(signaling, rtsp, args.mic, args.id)
    rtc_client.turn = args.turn
    rtc_client.turn_user = args.turn_user
    rtc_client.turn_passwd = args.turn_passwd
    rtc_client.stun = args.stun

    loop = asyncio.get_event_loop()
    try:
        print("========= RTSP ", rtsp)
        print("WebSocket server started")
        loop.run_until_complete(
            rtc_client.loop(signaling=signaling, room=args.room, display=args.name)
        )
    except Exception as e:
        print("------------------------Exception: ", e)
        if e.args:
            content = e.args[0]
        else:
            content = 'Unknown exception'
        send_msg_to_main('exception', content, args.id)
    finally:
        print("========= RTSP ", rtsp)
        print("WebSocket server stopped")
        # 销毁 RTC client
        loop.run_until_complete(rtc_client.destroy())
        # 关闭 WS
        loop.run_until_complete(signaling.close())
