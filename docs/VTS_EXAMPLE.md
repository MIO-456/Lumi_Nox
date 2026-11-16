# VTube Studio / Hiyori example

> **Read when**: connecting a user-installed VTS model to the public host.
> **Skip when**: using text/audio without an avatar.
> **Review**: when VTS APIs, sample-model terms or the host adapter change.

The public host can drive a selected model's mouth from the audio it actually
plays. VTS is optional and starts disabled. No model, private motion, voice,
existing streamer token, or character persona is included.

## Install and select a model

1. Install VTube Studio from its official distribution and open it yourself.
2. Use its Hiyori sample if available, or obtain Hiyori Momose from the
   [official Live2D sample page](https://www.live2d.com/en/learn/sample/momose-hiyori/).
   Follow VTS's [model-loading instructions](https://github.com/DenchiSoft/VTubeStudio/wiki/Loading-your-own-Models).
   The runtime model files, rather than the Cubism editor project alone, are
   needed. This repository does not download or redistribute them.
3. Read the model's [sample-material terms](https://www.live2d.com/en/learn/sample/model-terms/)
   and the linked Free Material License Agreement. Hiyori is a licensed sample,
   not an unrestricted open-source asset. Its individual terms prohibit changes
   to the character design. VTS's [application license](https://denchisoft.com/license/)
   is separate. Using this code does not replace either agreement.
4. Load the model in VTS, then enable the VTS API in its settings. The usual
   address is `ws://127.0.0.1:8001`; use the port shown by your own instance.

## Authorize and configure the public host

Enable VTS in the character's public-host configuration, enter its address, and
use the authorize action. In VTS, review and allow **Lumi Nox Public Host** by
**Lumi Nox Contributors**. Only this explicit authorization action can request a
new token. Ordinary session start reuses the public host's saved token and
reports an error if authorization is missing or revoked.

With no model ID configured, the connection permits discovery only. List the
models and choose the exact ID of the currently loaded model. Save that ID in
the character configuration. The host checks it before starting mouth output
and periodically while running. It does not load, replace, or unload models
automatically. A changed model or disconnected VTS stops avatar output with an
error; reconnect after fixing the setting.

Tokens are stored separately inside the public host's ignored local-data
directory, indexed by the VTS address. They are not returned as configuration
values or written to the log. Do not copy the private streamer's tokens into it.

## Mouth mapping and idle movement

Start with input parameter `MouthOpen` and gain `1`. In VTS's model settings,
check that this input drives the model's mouth-open Live2D output. VTS input
names and Live2D output names are different: sending `ParamMouthOpenY` as an API
input is generally incorrect. `MouthOpen` is normalized from 0 (closed) to 1
(open); adjust the output range and smoothing in VTS if needed.

First use the audio-only preview to check the voice; that preview deliberately
does not connect VTS. Then start a real session, select the character and submit
a short text with **Direct speech / 直接朗读**. The mouth should move while sound is playing, close
when playback ends, and close when the turn is interrupted. A successful API
response confirms protocol delivery, not a visibly correct model mapping.
Check the actual model on screen. Animations or expressions may override the
mapped output.

For idle breathing and blinking, use VTS **Auto-breath** and **Auto-blink** on
the appropriate model outputs. This release does not install a private motion
library, inject a made-up `Breath` input, or modify the model's mapping file.
See [official parameter setup](https://github.com/DenchiSoft/VTubeStudio/wiki/VTS-Model-Settings).

## Two characters and stopping

First verify one character. For two, use separate VTS instances with different
API ports and configure each character's model ID. Test A, then B: only the
speaking character's mouth should move. Two characters must not target the same
VTS endpoint. Do not run another plugin that controls the same mouth input.

Stopping invalidates pending mouth frames, attempts to close the mouth only if
the configured model is still loaded, and closes this plugin's connection. It
does not close VTS or unload your model. If the connection is lost, VTS releases
an injected input after it has not been updated for over one second; another
tracker or the default value then takes over. See the
[official API and injection rules](https://github.com/DenchiSoft/VTubeStudio#feeding-in-data-for-default-or-custom-parameters).

## Verification boundary

Automated fake-server checks cover authorization, response matching, model
checks, delayed mouth frames and cleanup. They do **not** verify Hiyori's visual
mapping, real VTS authorization, audible playback, or two real VTS instances.
Complete the single-character session and stop checks above, then the two-port
check, before treating your own setup as ready for a stream.
