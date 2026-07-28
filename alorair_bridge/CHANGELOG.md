# Changelog

## 1.6.2

Baseline release — changelog starts here. The bridge is feature-complete for
the Sentinel HDi65S (and its same-module BaseAire / Abestorm twins):

- **Status:** power (tracks the front panel, incl. the post-power-loss start
  delay), target humidity, normal/continuous mode, current humidity,
  temperature, coil temperature, grains-per-pound, compressor / fan / pump,
  and total working hours.
- **Control from Home Assistant:** on/off, target humidity (35–90%),
  normal/continuous mode, purge (one-shot pump-out), and locate.
- **MQTT autodiscovery** for the humidifier entity plus all sensors and
  controls; availability tied to both the device connection and the bridge.
- **In-app documentation** (Documentation tab) covering configuration and the
  network redirect (DNS override — including the Dnsmasq add-on — or DNAT).

Future releases will be listed above this entry with their changes.
