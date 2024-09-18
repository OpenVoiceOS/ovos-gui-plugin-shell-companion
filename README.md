# OVOS Shell companion

provides various bus APIs that integrate with [ovos-shell](https://github.com/OpenVoiceOS/ovos-shell)
    
    - color scheme manager
    - notifications widgets
    - configuration provider  (settings UI)
    - brightness control  (night mode etc)
    

## Features

```json
{
  "gui": {
     "ovos-gui-plugin-shell-companion": {
       "sunrise_time": "auto",
       "sunset_time": "auto",
       "default_brightness": 100,
       "night_default_brightness": 70,
       "low_brightness": 20,
       "auto_dim_seconds": 60,
       "auto_dim": false,
       "auto_nightmode": false
     }
  }
}
```


### Night mode

night mode will perform actions based on sunset/sunrise times

- the homescreen changes to a simple clock with white text on a black background.
- default brightness is reduced.
- auto-dim is enabled

`sunrise_time` and `sunset_time` will be automatically calculated based on location if set to `"auto"`, specific times can be explicitly set with the format `"HH:MM"`, eg. if you are an early riser you may want `"sunrise_time": "05:30"`

brightness level during nighttime can be set via `"night_default_brightness"`

### Auto Dim

auto-dim will lower the screen brightness after 60 seconds of inactivity, until the user interacts with the GUI or talks to the OVOS device

brightness level when idle can be set via `"low_brightness"`

auto-dim can be enabled at all times by setting `"auto_dim": true` in your config


## DEPRECATION WARNING

> in **ovos-core version 0.0.7** the bus apis provided by this repo used to be several individual PHAL plugins

the following packages have been deprecated in favor of this repo:
- [ovos-PHAL-plugin-dashboard](https://github.com/OpenVoiceOS/ovos-PHAL-plugin-dashboard) <- DEPRECATED, community maintained, no official replacement, [removed from ovos-shell](https://github.com/OpenVoiceOS/ovos-gui/pull/10)
- [ovos-PHAL-plugin-configuration-provider](https://github.com/OpenVoiceOS/ovos-PHAL-plugin-configuration-provider) <- now part of this repo
- [ovos-PHAL-plugin-notification-widgets](https://github.com/OpenVoiceOS/ovos-PHAL-plugin-notification-widgets) <- now part of this repo
- [ovos-PHAL-plugin-brightness-control-rpi](https://github.com/OpenVoiceOS/ovos-PHAL-plugin-brightness-control-rpi) <- now part of this repo
- [ovos-PHAL-plugin-color-scheme-manager](https://github.com/OpenVoiceOS/ovos-PHAL-plugin-color-scheme-manager) <- now part of this repo
