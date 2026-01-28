# **Dymo Scale MQTT Bridge**

A lightweight Python bridge to connect **DYMO M-Series USB Shipping Scales** to **Home Assistant** (or any MQTT broker).

This project is designed to run on a Raspberry Pi (ideal for the Pi Zero W) using **BalenaCloud** or Docker, making it a robust, "plug-and-play" appliance.

## **Features**

* **Plug & Play:** Automatically detects Dymo USB scales.  
* **Auto-Discovery:** Automatically creates sensors in Home Assistant (Weight, Status, Connectivity).  
* **Smart Availability:** Correctly reports the scale as "Offline" if the scale turns off or goes to sleep, and "Online" when it wakes up.  
* **Status Reporting:** Reports detailed status (Stable, In Motion, Zeroing, Overload, Under Zero).  
* **GPIO Support:** Optional support for physical buttons (Tare/Send) using the Pi's GPIO pins.

## **Supported Hardware**

This project works with DYMO M-Series Digital Mailing Scales that feature a USB interface:

* **Dymo M5** (5 kg / 11 lb)  
* **Dymo M10** (10 kg / 22 lb)  
* **Dymo M25** (25 kg / 55 lb)

*Note: While many S-Series (Heavy Duty) scales share the same HID protocol, this project is tested specifically against the M-Series.*

## **Deployment**

### **Option 1: BalenaCloud (Recommended)**

1. Create a generic Raspberry Pi application in BalenaCloud.  
2. Push this repository to your Balena application.  
3. Set the following **Device Variables** in the Balena Dashboard:

| Variable | Default | Description |
| :---- | :---- | :---- |
| MQTT\_BROKER | homeassistant.local | IP or Hostname of your MQTT Broker |
| MQTT\_PORT | 1883 | Port (usually 1883\) |
| MQTT\_USER | *(None)* | MQTT Username (Optional) |
| MQTT\_PASS | *(None)* | MQTT Password (Optional) |
| ENABLE\_BUTTONS | false | Set to true to enable GPIO buttons |

### **Option 2: Docker / Docker Compose**

You can run this container on any Linux machine with USB access.

version: '2'  
services:  
  dymo-bridge:  
    build: .  
    privileged: true \# Required for USB access  
    network\_mode: host  
    environment:  
      \- MQTT\_BROKER=192.168.1.10  
      \- MQTT\_USER=my\_user  
      \- MQTT\_PASS=my\_pass

## **License**

This project is licensed under the MIT License \- see the [LICENSE](https://www.google.com/search?q=LICENSE) file for details.