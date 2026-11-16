<p align="center">
  <img src="data/assets/common/icons/work.mmoe.panasonic.PostureManager.png" alt="Panasonic 2-in-1 Tablet Posture Management Tool" width="144" height="144">
</p>

# Panasonic 2-in-1 Tablet Posture Management Tool

[简体中文](README.md) · English

This cross-platform tool recognizes how you position your convertible laptop and automatically rotates the built-in screen on **Windows and Linux**.

You can prop a Panasonic Let's note QV convertible up like a tent or rest it keyboard-side down as a stand, but its built-in hardware and software don't respond correctly to either setup. We built this app with two everyday uses in mind. In tent mode, the picture should rotate 180° so it appears upright. In stand mode, tapping a text field should bring up the system’s on-screen keyboard when you need to type.

**Current version: 3.1.0** · [Download an installer](https://github.com/adogecheems/panasonic-posture-manager/releases)

## Supported devices and installation

The tool has been verified on the Panasonic Let's note QV8, QV9 and QV1 series. On other 360° convertibles, available features depend on the hardware. The app is available in English and Simplified Chinese.

| System | How to install |
| --- | --- |
| Windows 10 or 11 (64-bit) | Download the English or Simplified Chinese installer and follow the prompts. Restart if prompted. |
| Linux (64-bit Intel or AMD, GNOME desktop) | Install the package for Arch Linux, Ubuntu, Debian or Fedora, then sign out and back in. |

KDE Plasma 6 support is experimental and has yet to be verified on hardware.

## Everyday use

The app starts at sign-in by default. Once the device is steady, the built-in screen follows its posture.

| How you position the laptop | What the screen does |
| --- | --- |
| Laptop, keyboard facing up | Stays in normal landscape orientation |
| Tent, propped up in an inverted V | Rotates 180° |
| Stand, keyboard facing down | When you tap a text field, your system’s touch input settings determine whether the on-screen keyboard appears. The screen stays in landscape. |
| Tablet, screen folded all the way back | The operating system takes over automatic rotation |

In tent and stand modes, Linux disables the built-in keyboard and touchpad. Windows keeps both available. When the screen is folded all the way back, the device and operating system handle input.

- **Choose a mode.** Under **Settings → Mode selection**, choose Automatic, Laptop, Tent or Stand. To use the device as a tablet, fold the screen all the way back.
- **Confirm a switch.** By default, the app asks before switching from laptop to tent. Let the device settle, then click the notification or confirm on the home page. You can change the tent and stand prompts separately under **Settings → Automatic mode**.
- **Quit on Windows.** Closing the window leaves the app in the system tray. Choose **Quit** from the tray menu and wait for the app to hand control back to Windows.

## Troubleshooting

| Symptom | What to try |
| --- | --- |
| The app reports that the background service is unavailable | Reopen the app. If you just installed it on Linux, sign out and back in first. On Windows, if it still can't connect, run the installer, choose Repair and restart. |
| The app detects the wrong posture | Open the device as a laptop and place the base on a level desk. Leave it still for about two seconds, then choose **Calibrate** under **Settings → Sensor**. |
| The screen doesn't rotate | Click **Retry**. If an external display is connected, check display mirroring and arrangement. |
| The keyboard or touchpad stays disabled | Return the device to laptop posture and choose **Automatic** under **Settings → Mode selection**. |
| The device doesn't enter tablet posture when fully folded | Unfold the screen, then fold it all the way back again. |
| The built-in keyboard cannot wake Windows after the screen turns off in tablet posture | Briefly press the power button to wake it. The built-in keyboard may not wake the device when fully folded back. If it still cannot wake the device after unfolding into laptop posture while the screen is off, further investigation is needed; note whether typing works after waking with the power button. |

[Installation, removal and maintenance guide (Chinese)](MAINTENANCE.md) · [License (AGPL-3.0-or-later)](LICENSE)
