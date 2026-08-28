# MAA unattended recovery scope

Version: 3

This file is the operational contract and FAQ for the Codex recovery agent. It
is tracked in Git, its SHA-256 is included in every recovery incident, and the
controller independently checks the result. The recovery process is
intentionally started with `--dangerously-bypass-approvals-and-sandbox`: there
is no Codex sandbox and no command approval gate. This document defines the
operational mission and stopping conditions; it is not a technical sandbox.

## Mission and terminal conditions

After a managed `full` launcher run fails, diagnose the real cause, repair it,
and keep trying until one **new full launcher run** succeeds. Every managed
stage is reentrant, including daily, so partial or completed earlier stages may
be replayed as part of a new full run. A recovery is not complete merely
because Waydroid starts, the game reaches its home screen, one
MAA task succeeds, or a command returns zero.

There are only three valid terminal states:

1. `recovered`: a new run has every expected phase in an accepted terminal
   state and its append-only `run-finished` event says `success`.
2. `scope-blocked`: an explicit hard boundary below prevents that result.
3. `failed`: the recovery process itself irrecoverably malfunctioned. Ordinary
   DNS errors, timeouts, transient upstream failures, and a first unsuccessful
   repair attempt are not terminal failures; continue investigating and retry.

The agent must run the retry command supplied in the incident evidence. It must
preserve `MAA_RECOVERY_ACTIVE=true` and must not start, stop, or restart the
outer systemd unit that is currently hosting it. The controller, not the model,
makes the final success decision.

## In scope

The agent may use unrestricted user-level shell commands and network access to:

- inspect project logs, screenshots, supervisor histories, Android ANR files,
  `journalctl`, process state, sockets, routes, resolver state, and systemd user
  state;
- inspect host and Waydroid DNS separately and probe the exact game/API/CDN
  hosts found in logs from both environments;
- use ADB and Waydroid commands, inspect Android properties/logcat/UI state,
  wait for transient startup work, and click a known benign retry/confirm/Wait
  dialog;
- complete both kinds of official game update: in-app resource/hot updates and
  an official CN client APK update for `com.hypergryph.arknights`;
- resolve a fresh APK URL from the official dynamic endpoint
  `https://ak.hypergryph.com/downloads/android_lastest`, download it only over
  HTTPS through Hypergryph/HyCDN redirects, and replace the existing package
  with `adb install --no-streaming -r` (or `adb install -r` if required) so app
  data is preserved; never reuse a version-specific CDN URL from an old run;
- force-stop and relaunch `com.hypergryph.arknights`, restart the Waydroid
  session, and when necessary restart the Waydroid container service;
- make a reversible, narrowly targeted, temporary DNS/network adjustment using
  supported system interfaces, including non-interactive `sudo -n` when it is
  already authorized; record the before/after state and restore the temporary
  change after verification;
- retry downloads and endpoints with bounded backoff, consult public technical
  documentation through shell network tools, and run the supplied full
  launcher repeatedly;
- create recovery notes only below ignored runtime state (`var/`) when useful.

An unrestricted shell is a capability, not permission to broaden the mission.
Use the smallest reversible action that can distinguish or repair the current
fault, and verify each material change.

## Hard boundaries

Return `scope-blocked` instead of crossing any of these boundaries:

- interactive account login, SMS/email verification, CAPTCHA, accepting new
  legal terms, or any other identity/consent step;
- a six-star recruitment decision or another game choice explicitly reserved
  for the operator;
- installing an APK for any package other than `com.hypergryph.arknights`,
  using an unofficial/mirrored APK, changing game account/client channel/region,
  accepting a signing-certificate mismatch, forcing a downgrade, purchasing
  anything, or using Originite Prime or ordinary medicine outside the checked
  launcher policy;
- clearing app data/cache, reinstalling the game, deleting user data, changing
  credentials, or bypassing authentication/security controls;
- editing tracked source, Git history, task policy, the recovery scope, runtime
  receipts, or append-only audit events during unattended recovery;
- persistent host DNS/firewall/routing/package changes, disabling security
  controls, or a privileged action that requires an interactive password;
- exceeding the protected game-day or scheduler time window.

Do not inspect or disclose credentials or unrelated personal files. Do not use
the shell to weaken the controller's verification or to manufacture success
evidence.

## Policy-resolved game conditions

No saved proxy for a candidate stage and a stage that is not currently open are
normal game-state limits, not infrastructure failures. For an automatic run,
let the launcher reject that candidate and try its next candidate or
regular-stage fallback. They become a whole-run `scope-blocked` result when the
original request explicitly required that exact stage, or when every
policy-authorized candidate/fallback is unavailable and no compliant full-run
result can be produced.

Both an in-app resource update and an official CN APK update are recovery work,
not scope blockers. If an Android store UI requests a human account, do not use
that store: use the official dynamic APK endpoint and ADB replacement install.
Only a package/channel/signature mismatch that cannot be updated without
uninstalling or clearing data is `unsupported-client`.

## FAQ: forced APK/client update

The official dynamic URL is a resolver, not a pinned APK. Resolve it fresh for
every incident and require every redirect to remain HTTPS and on an official
Hypergryph/HyCDN host. Do not use TapTap, Bilibili, emulator-vendor mirrors,
search results, or an APK cached by an earlier recovery.

1. Record the installed package, version code/name, signing information exposed
   by Android, current data path, available host/Waydroid space, and the update
   prompt or log evidence.
2. Download the fresh official APK below ignored runtime state (`var/`), with
   resume and bounded retry when useful. Check the final URL, HTTP status,
   content type/length, ZIP integrity, and a SHA-256 digest before installation.
3. Stop only `com.hypergryph.arknights`. Replace it with
   `adb install --no-streaming -r APK`, falling back to `adb install -r APK` if
   required. Do not use `-d`, `-t`, uninstall, `pm clear`, or removal of the
   existing package/data.
4. Android Package Manager must accept the existing signing identity. Treat
   `INSTALL_FAILED_UPDATE_INCOMPATIBLE` as `unsupported-client`; do not bypass
   it by uninstalling. On a downgrade result, refresh the official resolver and
   wait/retry rather than forcing the downgrade.
5. Confirm that the official package is present at the new version and still
   has its data, then launch it. Complete any subsequent in-app resource update,
   handling benign update/retry/Wait dialogs and connectivity faults in scope.
6. Delete only the downloaded recovery APK after it is no longer needed, then
   run the supplied complete launcher and continue until its new audit succeeds.

## FAQ: stuck at “正在获取更新…” or Android ANR

Typical evidence is a game screen stuck on “正在获取更新…” followed by an
Android “isn't responding” dialog. A successful generic host probe or a HEAD
request to `ak.hypergryph.com` does not prove that Android can resolve and reach
the actual update/CDN host.

Use this ladder, checking fresh evidence after each step:

1. Inspect the latest interface screenshots, `dumpsys window/activity`, logcat,
   the Waydroid ANR report, and the MAA log. Determine whether this is update
   download, DNS, TLS/routing, renderer, login, or another dialog.
2. Compare resolver properties/routes and exact-host DNS/HTTPS results on the
   host and inside Waydroid. Test hosts observed in logs rather than guessing a
   single canonical domain.
3. Allow a reasonable update wait. On an ANR dialog caused by the pending
   update, choose **Wait**, not **Close app**, then observe whether network and
   UI progress resume. Handle a benign game retry/confirm popup by clicking it.
4. If the process is still wedged, force-stop and relaunch only the official CN
   package. Preserve app data.
5. If Android networking or the session is stale, restart the Waydroid session;
   restart the container service only when evidence warrants it and it is
   available non-interactively.
6. Apply a reversible DNS/network workaround only after identifying the failed
   layer. Record and later restore any temporary host change.
7. Run the supplied full launcher command. If it fails, inspect that new run's
   fresh evidence and continue the ladder. Never treat a partial MAA success as
   completion.

## Success proof

For `recovered`, report the exact retry command, its zero exit status, the new
run ID, and its audit path. The controller then independently requires:

- the new run ID differs from the failed parent run;
- the new run is `full`, uses the clean Git HEAD recorded when recovery started,
  and has one accepted terminal result for every expected phase;
- its final hash-chained event reports process status 0 and overall success;
- the repository is still clean.

If any check fails, continue recovery or report the applicable scope blocker;
do not claim success.
