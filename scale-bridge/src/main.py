import usb.core
import usb.util
import paho.mqtt.client as mqtt
import time
import json
import sys
import signal
import os

# --- BALENA CONFIGURATION (Via Environment Variables) ---
MQTT_BROKER = os.getenv('MQTT_BROKER', 'homeassistant.local')
MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
MQTT_USER = os.getenv('MQTT_USER', None)
MQTT_PASS = os.getenv('MQTT_PASS', None)

DEFAULT_BUTTONS = {17: "Button 1", 27: "Button 2"}
button_env = os.getenv('BUTTON_MAP')
if button_env:
    try:
        loaded_map = json.loads(button_env)
        BUTTON_MAP = {int(k): v for k, v in loaded_map.items()}
    except Exception as e:
        print(f"Error parsing BUTTON_MAP env var: {e}")
        BUTTON_MAP = DEFAULT_BUTTONS
else:
    BUTTON_MAP = DEFAULT_BUTTONS

BUTTON_DEBOUNCE = 0.05
VENDOR_ID = 0x0922

# --- GLOBALS ---
device = None
endpoint = None
mqtt_client = None
running = True
active_buttons = []
GPIO_AVAILABLE = False

try:
    from gpiozero import Button
    GPIO_AVAILABLE = True
except ImportError:
    print("GPIO Zero not found")

TOPIC_BRIDGE_STATUS = "dymo/bridge/status"
TOPIC_SCALE_STATUS = "dymo/scale/status"

def signal_handler(sig, frame):
    global running
    print("Stopping...")
    running = False

def connect_mqtt():
    client = mqtt.Client(client_id=f"dymo_balena_{os.getenv('BALENA_DEVICE_UUID', 'local')[:7]}")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    
    client.will_set(TOPIC_BRIDGE_STATUS, "offline", retain=True)
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        print(f"Connected to MQTT Broker at {MQTT_BROKER}")
        return client
    except Exception as e:
        print(f"Failed to connect to MQTT: {e}")
        return None

def setup_scale():
    global device, endpoint
    try:
        device = usb.core.find(idVendor=VENDOR_ID)
    except Exception:
        device = None

    if device is None:
        return False

    print(f"Scale found: {device}")

    # Standard Linux cleanup
    if device.is_kernel_driver_active(0):
        try:
            device.detach_kernel_driver(0)
        except usb.core.USBError:
            pass

    try:
        device.set_configuration()
    except usb.core.USBError:
        pass

    cfg = device.get_active_configuration()
    intf = cfg[(0,0)]

    endpoint = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: \
            usb.util.endpoint_direction(e.bEndpointAddress) == \
            usb.util.ENDPOINT_IN
    )
    return True if endpoint else False

def publish_discovery(client):
    device_info = {
        "identifiers": [f"dymo_balena_{os.getenv('BALENA_DEVICE_UUID', 'local')}"],
        "name": "Dymo M2 Scale",
        "manufacturer": "Dymo",
        "model": "Balena Bridge",
        "sw_version": "1.0"
    }

    # 1. Bridge Status
    topic_bridge = "homeassistant/binary_sensor/dymo_scale/bridge/config"
    payload_bridge = {
        "name": "Dymo Bridge Status",
        "state_topic": TOPIC_BRIDGE_STATUS,
        "unique_id": f"dymo_bridge_{os.getenv('BALENA_DEVICE_UUID', 'local')}",
        "device_class": "connectivity",
        "payload_on": "online",
        "payload_off": "offline",
        "device": device_info
    }
    client.publish(topic_bridge, json.dumps(payload_bridge), retain=True)

    # 2. Scale Entity
    topic_scale = "homeassistant/sensor/dymo_scale/config"
    payload_scale = {
        "name": "Shipping Scale",
        "state_topic": "dymo/scale/weight",
        "availability": [
            {"topic": TOPIC_BRIDGE_STATUS},
            {"topic": TOPIC_SCALE_STATUS}
        ],
        "availability_mode": "all",
        "unit_of_measurement": "g",
        "icon": "mdi:scale-balance",
        "unique_id": f"dymo_scale_{os.getenv('BALENA_DEVICE_UUID', 'local')}",
        "device": device_info,
        "value_template": "{{ value_json.weight }}",
        "json_attributes_topic": "dymo/scale/weight"
    }
    client.publish(topic_scale, json.dumps(payload_scale), retain=True)

    # 3. Buttons
    if GPIO_AVAILABLE:
        for pin, name in BUTTON_MAP.items():
            safe_id = name.lower().replace(" ", "_")
            topic_btn = f"homeassistant/binary_sensor/dymo_scale/{safe_id}/config"
            payload_btn = {
                "name": f"Dymo {name}",
                "state_topic": f"dymo/scale/button/{safe_id}",
                "unique_id": f"dymo_btn_{pin}_{os.getenv('BALENA_DEVICE_UUID', 'local')}",
                "availability_topic": TOPIC_BRIDGE_STATUS,
                "device_class": "connectivity", 
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device_info
            }
            client.publish(topic_btn, json.dumps(payload_btn), retain=True)

def on_button_press(btn):
    pin = btn.pin.number
    name = BUTTON_MAP.get(pin, f"Button {pin}")
    safe_id = name.lower().replace(" ", "_")
    topic = f"dymo/scale/button/{safe_id}"
    if mqtt_client: mqtt_client.publish(topic, "ON", retain=False)

def on_button_release(btn):
    pin = btn.pin.number
    name = BUTTON_MAP.get(pin, f"Button {pin}")
    safe_id = name.lower().replace(" ", "_")
    topic = f"dymo/scale/button/{safe_id}"
    if mqtt_client: mqtt_client.publish(topic, "OFF", retain=False)

def setup_buttons():
    if not GPIO_AVAILABLE: return
    global active_buttons
    for pin in BUTTON_MAP:
        try:
            btn = Button(pin, pull_up=True, bounce_time=BUTTON_DEBOUNCE)
            btn.when_pressed = on_button_press
            btn.when_released = on_button_release
            active_buttons.append(btn)
            print(f"GPIO {pin} configured")
        except Exception as e:
            print(f"GPIO {pin} error: {e}")

def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    global mqtt_client, device
    
    print("Starting Dymo Balena Bridge...")
    mqtt_client = connect_mqtt()
    if not mqtt_client:
        print("Could not connect to MQTT. Exiting to trigger container restart.")
        sys.exit(1)

    mqtt_client.publish(TOPIC_BRIDGE_STATUS, "online", retain=True)
    mqtt_client.publish(TOPIC_SCALE_STATUS, "offline", retain=True)
    publish_discovery(mqtt_client)
    setup_buttons()

    last_weight = -1
    last_status = -1
    scale_online = False

    while running:
        try:
            if device is None:
                if setup_scale():
                    print("Scale Connected")
                    mqtt_client.publish(TOPIC_SCALE_STATUS, "online", retain=True)
                    scale_online = True
                else:
                    if scale_online:
                         print("Scale Disconnected")
                         mqtt_client.publish(TOPIC_SCALE_STATUS, "offline", retain=True)
                         scale_online = False
                    time.sleep(5)
                    continue

            try:
                data = device.read(endpoint.bEndpointAddress, endpoint.wMaxPacketSize, timeout=1000)
            except usb.core.USBError as e:
                if e.errno == 110: continue
                if e.errno == 19:
                    device = None
                    continue
                device = None
                continue

            if len(data) >= 6:
                offset = 0
                if data[2] in [2, 11, 12]: offset = 0
                elif data[1] in [2, 11, 12]: offset = -1
                
                status = data[offset+1]
                unit_type = data[offset+2]
                scaling = data[offset+3]
                if scaling > 127: scaling -= 256
                
                raw_val = data[offset+4] + (data[offset+5] << 8)
                weight = raw_val * (10 ** scaling)
                if unit_type in [11, 12]: weight = weight * 28.3495
                weight = round(weight, 1)

                if weight != last_weight or status != last_status:
                    payload = {"weight": weight, "status": "Stable" if status == 4 else "Unstable"}
                    mqtt_client.publish("dymo/scale/weight", json.dumps(payload), retain=True)
                    last_weight = weight
                    last_status = status

            time.sleep(0.1)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(1)

    mqtt_client.publish(TOPIC_BRIDGE_STATUS, "offline", retain=True)
    mqtt_client.loop_stop()

if __name__ == "__main__":
    main()