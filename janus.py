import argparse
import asyncio
import logging
import platform
import random
import string
import websockets
import json
import attr

from websockets.exceptions import ConnectionClosed
from aiortc.contrib.media import MediaPlayer, MediaRecorder, MediaRelay
from collections import OrderedDict
from h264track import H264EncodedStreamTrack, FFmpegH264Track
from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.rtcrtpparameters import RTCRtpCodecCapability
from transformer import VideoTransformTrack
from h264player import GstH264Camera, StreamPlayer

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
                await asyncio.sleep(30)
                transaction = transaction_id()
                await self.conn.send(json.dumps({
                    "janus": "keepalive",
                    "session_id": self.session,
                    "handle_id": self.handle,
                    "transaction": transaction
                }))
            except KeyboardInterrupt:
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
        else:
            return raw


class WebRTCClient:
    def __init__(self, id_, signaling: JanusGateway, rtsp):
        self.id_ = id_
        self.signaling = signaling
        self.rtsp = rtsp
        self.pc = None
        self.camera = None
        self.stream_player = None
        self.relay: MediaRelay = MediaRelay()

    async def destroy(self):
        if self.camera is not None:
            self.camera.stop()
        if self.stream_player is not None:
            self.stream_player.stop()
        await self.signaling.leave()
        await self.pc.close()

    async def handle_plugin_data(self, data):
        print("handle plugin data: \n", data)

        if data.jsep is not None:
            await self.handle_sdp(data.jsep)
        if data.data is not None:
            events_type = data.data["videoroom"]
            if events_type == "joined":
                await self.publish()
                publishers = data.data["publishers"]
                print("Publishes in the room: \n")
                for publisher in publishers:
                    print("id: %(id)s, display: %(display)s" % publisher)

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
        pc = RTCPeerConnection()
        self.pc = pc

        # configure media
        if self.rtsp is not None:
            # for testing switch camera
            if platform.system() == "Darwin":
                player = MediaPlayer(':0', format='avfoundation')
            elif platform.system() == "Linux":
                player = MediaPlayer("hw:2", format="alsa")
            else:
                player = MediaPlayer("default", format="dshow")

            if player.audio is not None:
                pc.addTrack(player.audio)

            player = StreamPlayer(self.rtsp)
            video_track = FFmpegH264Track(player)
            self.stream_player = player
            # self.camera = GstH264Camera(video_track, self.rtsp)

            # video_track = VideoTransformTrack(self.relay.subscribe(video_track), transform="rotate")
            pc.addTrack(video_track)
        else:
            raise Exception("No Media Input! Stop Now.")

        # send offer
        await pc.setLocalDescription(await pc.createOffer())

        request = {"request": "configure", "audio": True, "video": True}
        sdp = {"sdp": pc.localDescription.sdp, "trickle": False, "type": pc.localDescription.type}
        await self.signaling.sendmessage(request, sdp)

    async def loop(self, signaling, room, display, id):
        await signaling.connect()
        await signaling.attach("janus.plugin.videoroom")

        loop = asyncio.get_event_loop()
        loop.create_task(signaling.keepalive())

        message = {"request": "join", "ptype": "publisher", "room": room, "pin": str(room), "display": display,
                       "id": int(id)}
        await signaling.sendmessage(message)

        assert signaling.conn

        while True:
            try:
                msg = await signaling.recv()
                if isinstance(msg, PluginData):
                    await self.handle_plugin_data(msg)
                elif isinstance(msg, Media):
                    print(msg)
                elif isinstance(msg, WebrtcUp):
                    print(msg)
                elif isinstance(msg, SlowLink):
                    print(msg)
                elif isinstance(msg, HangUp):
                    print(msg)
                elif not isinstance(msg, Ack):
                    print(msg)
            except (KeyboardInterrupt, ConnectionClosed):
                return


def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Janus")
    parser.add_argument("url", help="Janus root URL, e.g. ws://localhost:8188")
    parser.add_argument(
        "--room",
        type=int,
        default=1234,
        help="The video room ID to join (default: 1234).",
    ),
    parser.add_argument(
        "--name",
        default="LocalCamera",
        help="The name display in the room",
    ),
    parser.add_argument(
        "--id",
        help="The ID of the camera in the videoroom(publishId)",
    ),
    parser.add_argument("--play-from", help="Read the media from a file and sent it."),
    parser.add_argument("--record-to", help="Write received media to a file."),
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    print("Received Params:", args)

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    play_from = args.play_from

    # create media sink
    if args.record_to:
        recorder = MediaRecorder(args.record_to)
    else:
        recorder = None

    # create signaling client
    signaling = JanusGateway(args.url)

    # create webrtc client
    our_id = random.randrange(10, 10000)
    rtc_client = WebRTCClient(our_id, signaling, play_from)

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(
            rtc_client.loop(signaling=signaling, room=args.room, display=args.name, id=args.id)
        )
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping now!")
        if recorder is not None:
            loop.run_until_complete(recorder.stop())

        # 销毁 RTC client
        loop.run_until_complete(rtc_client.destroy())
        # 关闭 WS
        loop.run_until_complete(signaling.close())
