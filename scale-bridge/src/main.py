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
ENABLE_BUTTONS = os.getenv('ENABLE_BUTTONS', 'false').lower() == 'true'
BUTTON_MAP = {17: "Button 1", 27: "Button 2"}
BUTTON_DEBOUNCE = 0.05
VENDOR_ID = 0x0922

# Watchdog: How long (seconds) to wait for data before marking "Offline"
DATA_TIMEOUT = 5.0

# Mappings
STATUS_MAP = {
    1: "Fault",
    2: "Zeroing",
    3: "In Motion",
    4: "Stable",
    5: "Under Zero",
    6: "Overload"
}

UNIT_MAP = {
    2: "g",
    11: "oz",
    12: "lb",
    3: "kg"
}

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
    print("GPIO Zero library not found. Buttons disabled.")

TOPIC_BRIDGE_STATUS = "dymo/bridge/status"
TOPIC_SCALE_STATUS = "dymo/scale/status"

def signal_handler(sig, frame):
    global running
    print("Stopping...")
    running = False

# --- DISCOVERY LOGIC (Moved up for Callback Access) ---
def publish_discovery(client):
    uuid = os.getenv('BALENA_DEVICE_UUID', 'local')
    device_info = {
        "identifiers": [f"dymo_balena_{uuid}"],
        "name": "Dymo M2 Scale",
        "manufacturer": "Dymo",
        "model": "Balena Bridge",
        "sw_version": "1.4"
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

    # 2. Scale Weight
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

    # 3. Scale Display Unit
    topic_unit = "homeassistant/sensor/dymo_scale/display_unit/config"
    payload_unit = {
        "name": "Shipping Scale Unit",
        "state_topic": "dymo/scale/unit",
        "availability": [
            {"topic": TOPIC_BRIDGE_STATUS},
            {"topic": TOPIC_SCALE_STATUS}
        ],
        "availability_mode": "all",
        "icon": "mdi:ruler-square",
        "unique_id": f"dymo_unit_{uuid}",
        "device": device_info
    }
    client.publish(topic_unit, json.dumps(payload_unit), retain=True)

    # 4. Scale Status Text
    topic_status = "homeassistant/sensor/dymo_scale/status_text/config"
    payload_status = {
        "name": "Shipping Scale Status",
        "state_topic": "dymo/scale/weight",
        "availability": [
            {"topic": TOPIC_BRIDGE_STATUS},
            {"topic": TOPIC_SCALE_STATUS}
        ],
        "availability_mode": "all",
        "icon": "mdi:information-outline",
        "unique_id": f"dymo_status_{uuid}",
        "device": device_info,
        "value_template": "{{ value_json.status }}"
    }
    client.publish(topic_status, json.dumps(payload_status), retain=True)

    # 5. Buttons
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
            
    print("Discovery Config Published")

# --- MQTT CONNECTION ---

def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT Broker (RC: {rc})")
    # Mark Bridge as Online immediately upon connection
    client.publish(TOPIC_BRIDGE_STATUS, "online", retain=True)
    # Re-send discovery config to ensure HA sees us even if HA restarted
    publish_discovery(client)

def connect_mqtt():
    client_id = f"dymo_{os.getenv('BALENA_DEVICE_UUID', 'local')[:7]}"
    client = mqtt.Client(client_id=client_id)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    
    # Last Will
    client.will_set(TOPIC_BRIDGE_STATUS, "offline", retain=True)
    
    # Attach Callback
    client.on_connect = on_connect
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
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

    if device is None: return False

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
    if not GPIO_AVAILABLE or not ENABLE_BUTTONS: return
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
    if not mqtt_client: sys.exit(1)
        
    # NOTE: Discovery and Bridge Status are now handled in on_connect
    
    mqtt_client.publish(TOPIC_SCALE_STATUS, "offline", retain=True)
    setup_buttons()

    last_weight = -1
    last_status = -1
    last_unit = -1
    scale_online = False
    last_packet_time = 0
    zero_motion_start = 0

    while running:
        if device is None:
            if setup_scale():
                print("Scale USB Found (Waiting for data...)")
                last_packet_time = time.time() # Grace period on connect
            else:
                if scale_online:
                     print("Scale Disconnected")
                     mqtt_client.publish(TOPIC_SCALE_STATUS, "offline", retain=True)
                     scale_online = False
                     last_weight = -1
                     last_status = -1
                     last_unit = -1
                time.sleep(5)
                continue

        try:
            data = device.read(endpoint.bEndpointAddress, 8, timeout=1000)
            
            # Check if we actually received data bytes
            if len(data) > 0:
                last_packet_time = time.time()
                
                if len(data) >= 6:
                    offset = 0
                    if data[2] in [2, 11, 12]: offset = 0
                    elif data[1] in [2, 11, 12]: offset = -1
                    
                    status = data[offset+1]
                    unit_code = data[offset+2]
                    scaling = data[offset+3]
                    if scaling > 127: scaling -= 256
                    
                    raw_val = data[offset+4] + (data[offset+5] << 8)
                    weight = raw_val * (10 ** scaling)
                    
                    # Convert to Grams for the main sensor
                    if unit_code in [11, 12]: 
                        weight = weight * 28.3495
                    
                    weight = round(weight, 1)

                    if status == 5: weight = -abs(weight)
                    
                    # --- SOFT OFF DETECTION ---
                    # Dymo scales often report 0g "In Motion" (Status 3) repeatedly when soft-off/sleeping
                    if weight == 0 and status == 3:
                        if zero_motion_start == 0:
                            zero_motion_start = time.time()
                        
                        if time.time() - zero_motion_start > DATA_TIMEOUT:
                            # We have been 0g In Motion for too long -> Consider this "Offline"
                            if scale_online:
                                print(f"Scale Soft Off (0g In Motion > {DATA_TIMEOUT}s) - Status: Offline")
                                mqtt_client.publish(TOPIC_SCALE_STATUS, "offline", retain=True)
                                scale_online = False
                                last_weight = -1
                                last_status = -1
                                last_unit = -1
                            continue # Skip processing this packet
                    else:
                        zero_motion_start = 0

                    # --- ONLINE CHECK ---
                    if not scale_online:
                        print("Scale Active - Status: Online")
                        mqtt_client.publish(TOPIC_SCALE_STATUS, "online", retain=True)
                        scale_online = True

                    status_text = STATUS_MAP.get(status, f"Unknown ({status})")
                    unit_text = UNIT_MAP.get(unit_code, "unknown")

                    # Publish Weight/Status changes
                    if weight != last_weight or status != last_status:
                        print(f"Weight: {weight}g (Status: {status_text})")
                        payload = {"weight": weight, "status": status_text}
                        mqtt_client.publish("dymo/scale/weight", json.dumps(payload), retain=True)
                        last_weight = weight
                        last_status = status

                    # Publish Unit changes
                    if unit_code != last_unit:
                        print(f"Display Unit Changed: {unit_text}")
                        mqtt_client.publish("dymo/scale/unit", unit_text, retain=True)
                        last_unit = unit_code
            
        except usb.core.USBError as e:
            if e.errno == 110: 
                # Timeout is normal (no data sent)
                pass
            elif e.errno == 19:
                print("Device disconnected (Error 19)")
                device = None
            else:
                print(f"USB Error: {e}")
                device = None

        # Watchdog
        if scale_online and (time.time() - last_packet_time > DATA_TIMEOUT):
            print(f"No data for {DATA_TIMEOUT}s - Status: Offline")
            mqtt_client.publish(TOPIC_SCALE_STATUS, "offline", retain=True)
            scale_online = False
            last_weight = -1
            last_status = -1
            last_unit = -1
            zero_motion_start = 0

        time.sleep(0.1)

    if mqtt_client:
        mqtt_client.publish(TOPIC_BRIDGE_STATUS, "offline", retain=True)
        mqtt_client.loop_stop()

if __name__ == "__main__":
    main()