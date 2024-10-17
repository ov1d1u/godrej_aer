# Godrej Aer Smart Matic Integration for Home Assistant

This is a custom integration for Home Assistant to support Godrej Aer Smart Matic, a Bluetooth Low Energy (BLE) room freshener device. Using this integration, you can monitor and control your Godrej Aer Smart Matic directly from Home Assistant.

## Supported Features

This integration currently only supports the following features:

- Trigger the device via a `button` entity
- Battery level monitoring (in mV)

## Installation

### Manual Installation

1. **Download the Repository**: Clone or download this repository and place it within your Home Assistant `config` directory, making sure the final path is `/config/custom_components/godrej_aer`.

2. **Restart Home Assistant**: After adding the custom component to your setup, restart Home Assistant to ensure it finds the new integration.

3. **Add the Integration**: Navigate to the Home Assistant UI and go to **Settings > Devices & Services > Integrations** and look for "Godrej Aer Smart Matic". Follow the on-screen instructions to complete setup.

### Installation via HACS

1. **Open HACS**: Go to the Home Assistant UI, click on **HACS** in the sidebar, then click on **Integrations**.

2. **Add Custom Repository**: Click the three dots in the top right corner, then select **Custom repositories**.

3. **Enter the Repository URL**: Add `https://github.com/ov1d1u/godrej_aer` in the URL field, and select **Integration** as the category. Click **Add**.

4. **Install the Integration**: Search for "Godrej Aer Smart Matic" in HACS. Click on it, then click **Install**.

5. **Restart Home Assistant**: After installation, restart Home Assistant.

6. **Add the Integration through Home Assistant**: Navigate to **Settings > Devices & Services > Integrations** and look for "Godrej Aer Smart Matic". Follow the on-screen setup instructions.

## Configuration

To configure the Godrej Aer Smart Matic integration:

1. **Automatic Discovery**: The integration will attempt to automatically discover any Godrej Aer Smart Matic devices via Bluetooth.
   
2. **Manual MAC Address Entry**: If your device is not automatically discovered, you can manually enter the MAC address.

## Troubleshooting

- **Device Not Found**: Ensure that your Godrej Aer Smart Matic device is powered on and within Bluetooth range. This devices seems to have bad connectivity range, so make sure that the device is pretty close to a Bluetooth proxy.
- **Connection Issues**: If you're using Bluetooth proxies, sometimes it seems that they "hang" on the connection. Restart them and try again.

## License

This project is licensed under the terms of the Apache 2.0 license.

## Contributions

Contributions are welcome! Please provide bug reports or suggestions via GitHub issues or submit a pull request for any enhancements or fixes.

## Disclaimer

This project is not associated with Godrej Aer. All rights regarding the devices are reserved to Godrej Aer.

## Warning

This integration is very experimental and is not officially supported. Use at your own risk.