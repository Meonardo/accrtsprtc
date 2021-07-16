## accrtsprtc

A simple HTTP server for starting & stopping publishing RTSP stream to JanusVideoRoom.

## Notice

* aiortc is a copy from [jpiat](https://github.com/jpiat/aiortc) (brilliant work of avoiding unnecessary transcoding);

## Usage

For Server side:

* Run `python3 accrtsprtc.py --p {your port}`;
* Install some dependencies if any error pop out;
* Need Gstreamer framework(python binding).

For client side:

* Start publishing RTSP stream to Janus VideoRoom

  URI: 

  **POST** http://192.168.5.12:9001/camera/start

  Params: (**form**)

  |         |  Type  |              Example               |          Notice           |
  | :-----: | :----: | :--------------------------------: | :-----------------------: |
  |  rtsp   | String | rtsp://192.168.5.158:554/main.h264 |           Required            |
  | display | String |            IPCamera158             | Required |
  |  room   |  Int   |                1234                |           Required            |
  |  janus  |  String|                ws://127.0.0.1:8188                |           Required            |

  Response:

  `{
    "success": true,
    "code": 1,
    "data": "rtsp://192.168.5.201:554/main.h264 has been published to VideoRoom 1234"
  }`

* Stop publishing RTSP stream to Janus VideoRoom

  URI: 

  **POST** http://192.168.5.12:9001/camera/stop

  Params: (**form**)

  |      |  Type  |              Example               | Notice |
  | :--: | :----: | :--------------------------------: | :----: |
  | rtsp | String | rtsp://192.168.5.158:554/main.h264 |  Required  |
  |  room   |  Int   |                1234               |           Required            |

  Response:

  `{
  "success": true,
    "code": 1,
    "data": "rtsp://192.168.5.201:554/main.h264 Stopped!"
  }`

