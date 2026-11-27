class_name MqttLink
extends Node
## A minimal MQTT 3.1.1 client over WebSocket (QoS 0): enough for the simulator to act as virtual hardware
## on the same broker the real devices use. Works in the browser (it rides on the page's WebSocket).

signal connected
signal disconnected
signal message(topic: String, payload: String)

var is_open := false
var _ws := WebSocketPeer.new()
var _buffer := PackedByteArray()
var _topics := PackedStringArray()
var _hello_sent := false
var _ping_at := 0.0
var _was_connecting := false


func open(url: String, topics: PackedStringArray) -> void:
	_topics = topics
	_ws.supported_protocols = PackedStringArray(["mqtt"])
	_ws.inbound_buffer_size = 1 << 20  # a room definition with its variants is tens of kB; the default is 64 kB
	var err := _ws.connect_to_url(url)
	_was_connecting = err == OK
	if err != OK:
		disconnected.emit()


func publish(topic: String, payload: String) -> void:
	if not is_open:
		return
	var body := _string(topic)
	body.append_array(payload.to_utf8_buffer())
	_send(0x30, body)


func _process(_delta: float) -> void:
	if not _was_connecting:
		return
	_ws.poll()
	match _ws.get_ready_state():
		WebSocketPeer.STATE_OPEN:
			if not _hello_sent:
				_hello_sent = true
				var body := PackedByteArray([0x00, 0x04, 0x4D, 0x51, 0x54, 0x54, 0x04, 0x02, 0x00, 0x3C])  # "MQTT", v3.1.1, clean session, keepalive 60 s
				body.append_array(_string("the-core-sim-%d" % (randi() % 1000000)))
				_send(0x10, body)
			while _ws.get_available_packet_count() > 0:
				_buffer.append_array(_ws.get_packet())
			_parse()
			var now := Time.get_ticks_msec() / 1000.0
			if is_open and now > _ping_at:
				_ping_at = now + 20.0
				_ws.put_packet(PackedByteArray([0xC0, 0x00]))
		WebSocketPeer.STATE_CLOSED:
			_was_connecting = false
			if is_open or _hello_sent:
				is_open = false
			disconnected.emit()


func _parse() -> void:
	while _buffer.size() >= 2:
		var length := 0
		var multiplier := 1
		var i := 1
		while true:
			if i >= _buffer.size():
				return
			var b := _buffer[i]
			length += (b & 0x7F) * multiplier
			multiplier *= 128
			i += 1
			if b & 0x80 == 0:
				break
		if _buffer.size() < i + length:
			return
		var header := _buffer[0]
		var kind := header >> 4
		var packet := _buffer.slice(i, i + length)
		_buffer = _buffer.slice(i + length)
		match kind:
			2:  # CONNACK
				if packet.size() >= 2 and packet[1] == 0:
					is_open = true
					var sub := PackedByteArray([0x00, 0x01])
					for t in _topics:
						sub.append_array(_string(t))
						sub.append(0)
					_send(0x82, sub)
					connected.emit()
			3:  # PUBLISH
				var qos := (header >> 1) & 0x03
				var topic_len := (packet[0] << 8) | packet[1]
				var topic := packet.slice(2, 2 + topic_len).get_string_from_utf8()
				var start := 2 + topic_len + (2 if qos > 0 else 0)
				message.emit(topic, packet.slice(start).get_string_from_utf8())


func _string(s: String) -> PackedByteArray:
	var b := s.to_utf8_buffer()
	var out := PackedByteArray([b.size() >> 8, b.size() & 0xFF])
	out.append_array(b)
	return out


func _send(header: int, body: PackedByteArray) -> void:
	var out := PackedByteArray([header])
	var n := body.size()
	while true:
		var digit := n % 128
		n = n / 128
		if n > 0:
			digit |= 0x80
		out.append(digit)
		if n == 0:
			break
	out.append_array(body)
	_ws.put_packet(out)
