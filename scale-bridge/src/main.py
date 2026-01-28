import usb.core
import usb.util
import paho.mqtt.client as mqtt
import time
import json
import sys
import signal
import os

# --- CONFIGURATION ---
MQTT_BROKER = os.getenv('MQTT_BROKER', 'homeassistant.local')
MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
MQTT_USER = os.getenv('MQTT_USER', None)
MQTT_PASS = os.getenv('MQTT_PASS', None)

# GPIO Configuration
# Set 'ENABLE_BUTTONS' to 'true' in Balena Device Variables to use
ENABLE_BUTTONS = os.getenv('ENABLE_BUTTONS', 'false').lower() == 'true'
BUTTON_MAP = {17: "Button 1", 27: "Button 2"}
BUTTON_DEBOUNCE = 0.05
VENDOR_ID = 0x0922

# Status Mapping for Dymo Scales
STATUS_MAP = {
    1: "Fault",
    2: "Zeroing",
    3: "In Motion",
    4: "Stable",
    5: "Under Zero",
    6: "Overload"
}

# --- GLOBALS ---
device = None
endpoint = None
mqtt_client = None
running = True
active_buttons = []
GPIO_AVAILABLE = False

# Try imports
try:
    from gpiozero import Button
    GPIO_AVAILABLE = True
except ImportError:
    print("GPIO Zero library not found. Buttons disabled.")

TOPIC_BRIDGE_STATUS = "dymo/bridge/status"
TOPIC_SCALE_STATUS = "dymo/scale/status"

def signal_handler(sig, frame):
    global running
    print("Stopping...")
    running = False

def connect_mqtt():
    # Create unique ID based on Balena UUID
    client_id = f"dymo_{os.getenv('BALENA_DEVICE_UUID', 'local')[:7]}"
    client = mqtt.Client(client_id=client_id)
    
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    
    # Last Will: If script dies, mark Bridge as offline
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

    print(f"Scale found: {device.idVendor:04x}:{device.idProduct:04x}")

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
    uuid = os.getenv('BALENA_DEVICE_UUID', 'local')
    device_info = {
        "identifiers": [f"dymo_balena_{uuid}"],
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
        "unique_id": f"dymo_bridge_{uuid}",
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
        "unique_id": f"dymo_scale_{uuid}",
        "device": device_info,
        "value_template": "{{ value_json.weight }}",
        "json_attributes_topic": "dymo/scale/weight"
    }
    client.publish(topic_scale, json.dumps(payload_scale), retain=True)

    # 3. Buttons (Only if enabled)
    if GPIO_AVAILABLE and ENABLE_BUTTONS:
        for pin, name in BUTTON_MAP.items():
            safe_id = name.lower().replace(" ", "_")
            topic_btn = f"homeassistant/binary_sensor/dymo_scale/{safe_id}/config"
            payload_btn = {
                "name": f"Dymo {name}",
                "state_topic": f"dymo/scale/button/{safe_id}",
                "unique_id": f"dymo_btn_{pin}_{uuid}",
                "availability_topic": TOPIC_BRIDGE_STATUS,
                "device_class": "connectivity", 
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device_info
            }
            client.publish(topic_btn, json.dumps(payload_btn), retain=True)

# GPIO Callbacks
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
    if not GPIO_AVAILABLE or not ENABLE_BUTTONS:
        return
    global active_buttons
    print("Initializing buttons...")
    for pin in BUTTON_MAP:
        try:
            btn = Button(pin, pull_up=True, bounce_time=BUTTON_DEBOUNCE)
            btn.when_pressed = on_button_press
            btn.when_released = on_button_release
            active_buttons.append(btn)
        except Exception as e:
            print(f"Error setting up GPIO {pin}: {e}")

def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    global mqtt_client, device
    
    print("Starting Dymo Balena Bridge...")
    mqtt_client = connect_mqtt()
    if not mqtt_client:
        print("MQTT Connection failed. Exiting to restart container.")
        sys.exit(1)
        
    # Mark Bridge as Online
    mqtt_client.publish(TOPIC_BRIDGE_STATUS, "online", retain=True)
    
    # Mark Scale as Offline initially
    mqtt_client.publish(TOPIC_SCALE_STATUS, "offline", retain=True)

    publish_discovery(mqtt_client)
    setup_buttons()

    last_weight = -1
    last_status = -1
    scale_online = False

    while running:
        # 1. Device Connection
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

        # 2. Read Data
        try:
            # Dymo M25 sends 8 byte chunks usually, M10 sends 6. Requesting 8 is safe.
            data = device.read(endpoint.bEndpointAddress, 8, timeout=1000)
            
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
                
                if unit_type in [11, 12]: 
                    weight = weight * 28.3495
                
                weight = round(weight, 1)

                # Check for "Under Zero" status (5) and negate weight
                if status == 5:
                    weight = -abs(weight)

                # Map status code to string description
                status_text = STATUS_MAP.get(status, f"Unknown ({status})")

                if weight != last_weight or status != last_status:
                    print(f"Weight: {weight}g (Status: {status_text})")
                    payload = {"weight": weight, "status": status_text}
                    mqtt_client.publish("dymo/scale/weight", json.dumps(payload), retain=True)
                    last_weight = weight
                    last_status = status
            
        except usb.core.USBError as e:
            if e.errno == 110: 
                # Timeout is normal when scale is connected but idle/silent
                continue
            elif e.errno == 19:
                print("Device disconnected (Error 19)")
                device = None
            else:
                print(f"USB Error: {e}")
                device = None

        time.sleep(0.1)

    # Cleanup
    if mqtt_client:
        mqtt_client.publish(TOPIC_BRIDGE_STATUS, "offline", retain=True)
        mqtt_client.loop_stop()

if __name__ == "__main__":
    main()