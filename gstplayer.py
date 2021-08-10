
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst


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
