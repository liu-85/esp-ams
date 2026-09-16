"""
tests/mpy_stubs/umqtt/simple.py
===============================
`umqtt.simple.MQTTClient` 的桌面端桩实现，只在 PC / CI 上跑测试用。
所有网络调用都是空操作，只记录最后发送的内容，方便断言。
"""


class MQTTException(Exception):
    pass


class MQTTClient:
    def __init__(self, client_id, server, port=0, user=None, password=None,
                 keepalive=0, ssl=False, ssl_params=None, socket_timeout=None):
        self.client_id = client_id
        self.server = server
        self.port = port
        self.user = user
        self.password = password
        self.keepalive = keepalive
        self.ssl = ssl
        self._callback = None
        self._connected = False
        self.published = []      # [(topic, msg), ...]
        self.subscribed = []
        self.ping_count = 0

    def set_callback(self, f):
        self._callback = f

    def set_last_will(self, topic, msg, retain=False, qos=0):
        self.last_will = (topic, msg)

    def connect(self, clean_session=True):
        self._connected = True
        return 0

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def ping(self):
        if not self._connected:
            raise OSError("not connected")
        self.ping_count += 1

    def publish(self, topic, msg, retain=False, qos=0):
        self.published.append((topic, msg))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def wait_msg(self):
        return None

    def check_msg(self):
        return None

    def feed(self, data):
        if self._callback:
            return self._callback(None, data)
        return None


class MQTTClientSSL(MQTTClient):
    pass
