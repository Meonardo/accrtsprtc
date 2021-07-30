import av
import threading
import asyncio
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst


class StreamPlayer (threading.Thread):
    def __init__(self, rtsp, loop=asyncio.get_event_loop()):
        threading.Thread.__init__(self)
        # flag to indicate that the thread should stop
        self.isRunning = False
        self.rtsp = rtsp
        self.packets = asyncio.Queue()
        self.name = "StreamPlayer--" + rtsp
        self.loop = loop

        options = {'rtsp_transport': 'tcp'}
        self.container = av.open(rtsp, mode="r", metadata_encoding='utf-8', options=options)

    def run(self):
        """
        start the thread until a stop is requested.
        :return:
        """
        print("starting player thread")
        self.isRunning = True

        while self.isRunning:
            for i, packet in enumerate(self.container.demux()):
                try:
                    if packet.dts is None:
                        continue

                    if packet.stream.type == 'video':
                        if not self.packets.full():
                            asyncio.run_coroutine_threadsafe(self.packets.put(packet), self.loop)
                            # self.packets.put(packet)
                    if not self.isRunning:
                        break

                except InterruptedError:
                    self.isRunning = False
                    break
        self.container.close()

    def stop(self):
        if self.isRunning:
            self.isRunning = False
            self.container.close()
        print("H264 Streaming Player was shutdown!")


class GstH264Player:
    RTSP_PIPELINE = "rtspsrc location={} latency=0 ! rtph264depay ! queue ! h264parse ! video/x-h264,alignment=nal," \
                    "stream-format=byte-stream ! appsink emit-signals=True name=h264_sink "

    def __init__(self, output, rtsp):
        Gst.init(None)

        source = GstH264Player.RTSP_PIPELINE.format(rtsp)
        self.pipeline = Gst.parse_launch(source)
        self.output = output
        self.appsink = self.pipeline.get_by_name('h264_sink')
        self.appsink.connect("new-sample", self.on_buffer, None)
        self.pipeline.set_state(Gst.State.PLAYING)

    def on_buffer(self, sink, data) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if isinstance(sample, Gst.Sample):
            buffer = sample.get_buffer()
            byte_buffer = buffer.extract_dup(0, buffer.get_size())
            self.output.write(byte_buffer)
        return Gst.FlowReturn.OK

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)
        print("GstH264Player Streaming Player was shutdown!")
