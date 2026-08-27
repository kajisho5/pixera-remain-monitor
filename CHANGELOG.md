# Changelog

## 2.2.1
- Companion write test (`--companion-test`) and `SW-PIXERA-COMPANION-TEST.bat`

## 2.2.0
- Push remaining time and cue state into Bitfocus Companion custom variables

## 2.1.0
- Fix missing thumbnails: clips often have no label on real hardware, so the clip→resource
  mapping is now learned at runtime from the currently playing resource

## 2.0.0
- Monitor several PIXERA machines at once (comma-separated addresses)

## 1.9.0
- Now Playing strip: per-layer resource name and thumbnail; thumbnails on the timeline bar

## 1.8.x
- 30 Hz polling (was capped at 20 Hz by a sleep floor); uniform `HH:MM:SS:FF` readout

## 1.7.0
- Server-sent events instead of polling; smooth interpolated clock

## 1.6.0
- Enumerate every network adapter via `ipconfig`; same-segment verdict in diagnostics

## 1.4.0–1.5.0
- Auto-detect all three API framings; per-NIC source address selection

## 1.3.0
- LAN search across every adapter, PIXERA fingerprint ports, and API verification

## 1.1.0–1.2.0
- Settings window, LAN search, multi-port scan

## 1.0.0
- Initial release
