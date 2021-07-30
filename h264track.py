import asyncio
import math
import av
from queue import Queue
from struct import pack
from typing import Iterator, List, Tuple, Optional

from aiortc.mediastreams import EncodedStreamTrack
from aiortc.mediastreams import VIDEO_TIME_BASE, convert_timebase
from h264player import StreamPlayer
from av import Packet
from av.filter import Filter, Graph

PACKET_MAX = 1300

NAL_TYPE_FU_A = 28
NAL_TYPE_STAP_A = 24

NAL_HEADER_SIZE = 1
FU_A_HEADER_SIZE = 2
LENGTH_FIELD_SIZE = 2
STAP_A_HEADER_SIZE = NAL_HEADER_SIZE + LENGTH_FIELD_SIZE


class H264EncodedStreamTrack(EncodedStreamTrack):
    kind = "video"

    _start: float
    _timestamp: int

    def __init__(self, video_rate=30, clock_rate=90000) -> None:
        super().__init__()
        self.nal_queue = Queue(3)
        self._timestamp = 0
        self._frame_time = 1 / video_rate
        self._clock_rate = clock_rate
        self.nal_buffer = None
        print("Init h264 codec successfully.")

    def write(self, buf: bytes):
        if self.nal_buffer is None:
            self.nal_buffer = buf
        else:
            self.nal_buffer += buf
        if (
                len(self.nal_buffer) < 64
        ):  # Just making sure to pack SPS/PPS within a single buffer
            return
        if not self.nal_queue.full():
            self.nal_queue.put(self.nal_buffer)
        self.nal_buffer = None

    @staticmethod
    def _packetize_fu_a(data: bytes) -> List[bytes]:
        available_size = PACKET_MAX - FU_A_HEADER_SIZE
        payload_size = len(data) - NAL_HEADER_SIZE
        num_packets = math.ceil(payload_size / available_size)
        num_larger_packets = payload_size % num_packets
        package_size = payload_size // num_packets

        f_nri = data[0] & (0x80 | 0x60)  # fni of original header
        nal = data[0] & 0x1F

        fu_indicator = f_nri | NAL_TYPE_FU_A

        fu_header_end = bytes([fu_indicator, nal | 0x40])
        fu_header_middle = bytes([fu_indicator, nal])
        fu_header_start = bytes([fu_indicator, nal | 0x80])
        fu_header = fu_header_start

        packages = []
        offset = NAL_HEADER_SIZE
        while offset < len(data):
            if num_larger_packets > 0:
                num_larger_packets -= 1
                payload = data[offset: offset + package_size + 1]
                offset += package_size + 1
            else:
                payload = data[offset: offset + package_size]
                offset += package_size

            if offset == len(data):
                fu_header = fu_header_end

            packages.append(fu_header + payload)

            fu_header = fu_header_middle
        assert offset == len(data), "incorrect fragment data"

        return packages

    @staticmethod
    def _packetize_stap_a(
            data: bytes, packages_iterator: Iterator[bytes]
    ) -> Tuple[bytes, bytes]:
        counter = 0
        available_size = PACKET_MAX - STAP_A_HEADER_SIZE

        stap_header = NAL_TYPE_STAP_A | (data[0] & 0xE0)

        payload = bytes()
        try:
            nalu = data  # with header
            while len(nalu) <= available_size and counter < 9:
                stap_header |= nalu[0] & 0x80

                nri = nalu[0] & 0x60
                if stap_header & 0x60 < nri:
                    stap_header = stap_header & 0x9F | nri

                available_size -= LENGTH_FIELD_SIZE + len(nalu)
                counter += 1
                payload += pack("!H", len(nalu)) + nalu
                nalu = next(packages_iterator)

            if counter == 0:
                nalu = next(packages_iterator)
        except StopIteration:
            nalu = None

        if counter <= 1:
            return data, nalu
        else:
            return bytes([stap_header]) + payload, nalu

    @classmethod
    def _packetize(cls, packages: Iterator[bytes]) -> List[bytes]:
        packetized_packages = []

        packages_iterator = iter(packages)
        package = next(packages_iterator, None)
        while package is not None:
            if len(package) > PACKET_MAX:
                packetized_packages.extend(cls._packetize_fu_a(package))
                package = next(packages_iterator, None)
            else:
                packetized, package = cls._packetize_stap_a(package, packages_iterator)
                packetized_packages.append(packetized)

        return packetized_packages

    @staticmethod
    def _split_bitstream(buf: bytes) -> Iterator[bytes]:
        # TODO: write in a more pytonic way,
        # translate from: https://github.com/aizvorski/h264bitstream/blob/master/h264_nal.c#L134
        i = 0
        while True:
            while (buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01) and (
                    buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0 or buf[i + 3] != 0x01
            ):
                i += 1  # skip leading zero
                if i + 4 >= len(buf):
                    return
            if buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01:
                i += 1
            i += 3
            nal_start = i
            while (buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0) and (
                    buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01
            ):
                i += 1
                # FIXME: the next line fails when reading a nal that ends
                # exactly at the end of the data
                if i + 3 >= len(buf):
                    nal_end = len(buf)
                    buf_type = buf[nal_start] & 0x1F
                    if buf_type != 0x06:  # Make sure to discard SEI NALUs
                        yield buf[nal_start:nal_end]
                    return  # did not find nal end, stream ended first
            nal_end = i
            buf_type = buf[nal_start] & 0x1F
            if buf_type != 0x06:  # Make sure to discard SEI NALUs
                yield buf[nal_start:nal_end]

    async def recv_encoded(self, keyframe=False):
        while True:
            if self.nal_queue.empty():
                await asyncio.sleep(self._frame_time)
                continue
            nal = self.nal_queue.get()
            if (nal[4] & 0x1F) != 0x01 or not keyframe:
                break
        packets = self._packetize(self._split_bitstream(nal))
        if len(packets) > 0:
            self._timestamp += int(self._frame_time * self._clock_rate)
        timestamp = self._timestamp
        return packets, timestamp


class FFmpegH264Track(H264EncodedStreamTrack):
    kind = "video"

    def __init__(self, player: StreamPlayer):
        super().__init__()
        self.player = player
        player.start()

    async def recv_encoded(self, keyframe=False):
        while True:
            packet = await self.player.packets.get()
            if packet.dts is not None:
                break
        timestamp = convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)
        packets = self._packetize(self._split_bitstream(packet.to_bytes()))
        return packets, timestamp


def link_nodes(*nodes):
    for c, n in zip(nodes, nodes[1:]):
        c.link_to(n)


class MixGraph:
    def __init__(self, screen: StreamPlayer, cam: StreamPlayer):
        self.__cam = cam
        self.__screen = screen
        self.__graph = Graph()
        self.sink = None
        self.cam_src = None
        self.screen_src = None

        self.__configure()

    def __configure(self):
        if self.cam_src is not None or \
                self.screen_src is not None:
            return

        graph = self.__graph
        cam_src = graph.add_buffer(template=self.__cam.container.streams.video[0])
        screen_src = graph.add_buffer(template=self.__screen.container.streams.video[0])

        scale = graph.add("scale", "iw/3:ih/3")
        cam_src.link_to(scale, 0, 0)

        overlay = graph.add("overlay", "main_w-overlay_w-20:main_h-overlay_h-20")
        screen_src.link_to(overlay, 0, 0)
        scale.link_to(overlay, 0, 1)

        sink = graph.add('buffersink')
        overlay.link_to(sink, 0, 0)
        graph.configure()

        self.cam_src = cam_src
        self.screen_src = screen_src
        self.sink = sink


class MixTrack(H264EncodedStreamTrack):
    kind = "video"
    cam: StreamPlayer
    screen: Optional[StreamPlayer] = None
    graph: Optional[MixGraph] = None
    record_path = None
    __out_video_stream = None
    __record_container = None

    def __init__(self, cam: StreamPlayer, screen: StreamPlayer=None):
        super().__init__()
        self.cam = cam
        self.screen = screen
        cam.start()
        if screen is not None:
            screen.start()
            self.graph = MixGraph(cam=cam, screen=screen)
        self.__configure()

    def __configure(self):
        dir_path = "/Users/amdox/File/Combine/.recordings/out.ts"
        output = av.open(dir_path, "w")
        self.__record_container = output
        self.record_path = dir_path

        out_video_stream = output.add_stream('h264', rate=30, options={'profile': 'Main',
                                                                       'movflags': 'faststart'})

        out_video_stream.width = 1920
        out_video_stream.height = 1080
        out_video_stream.pix_fmt = 'yuv420p'
        self.__out_video_stream = out_video_stream

    async def recv_encoded(self, keyframe=False):
        packet_s: Optional[Packet] = None
        packet: Optional[Packet] = None

        if self.screen is not None:
            while True:
                packet_s = await self.screen.packets.get()
                if packet_s.dts is not None:
                    break
        while True:
            if self.cam is not None:
                packet = await self.cam.packets.get()
            else:
                break

            if packet.dts is not None:
                break

        if packet_s is not None and packet is not None:
            frame = await self.merge(packet_s, packet)
            if frame is not None:
                encoded_packets = self.__out_video_stream.encode(frame)
                if len(encoded_packets) > 0:
                    print("----------- Merged Frame ----------")
                    encoded_packet = encoded_packets[0]
                    self.__record_container.mux(encoded_packet)
                    packets = self._packetize(self._split_bitstream(encoded_packet.to_bytes()))
                    timestamp = convert_timebase(encoded_packet.pts, encoded_packet.time_base, VIDEO_TIME_BASE)
                else:
                    print("----------- Encode Failed ----------")
                    packets = self._packetize(self._split_bitstream(packet.to_bytes()))
                    timestamp = convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)
                return packets, timestamp
            else:
                print("----------- Camera Only ----------")
                timestamp = convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)
                packets = self._packetize(self._split_bitstream(packet.to_bytes()))
                return packets, timestamp
        else:
            timestamp = convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)
            packets = self._packetize(self._split_bitstream(packet.to_bytes()))
            return packets, timestamp

    async def merge(self, screen: Packet, cam: Packet):
        s = screen.decode()
        c = cam.decode()
        for frame_s, frame_c in zip(s, c):
            self.graph.screen_src.push(frame_s)
            self.graph.cam_src.push(frame_c)

            ofr = self.graph.sink.pull()
            ofr.pts = None
            return ofr



