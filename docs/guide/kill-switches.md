# Kill switches

## What this page is for

A kill switch stops a part of ATLAS when something unsafe happens. Examples are a write outside the sandbox, or an outgoing connection in Offline mode.

## Who uses it

Security engineer (read only) and audit.

## How to use it

1. Read which switch is on and why. Each stop has its own incident.
2. Read the proof that the incident needs.
3. Record the proof.
4. Resume. ATLAS refuses until the proof is complete.

## Drills

Audit can test a switch without a real problem. This is a drill.

1. Select **Run drill** on a switch card and confirm.
2. The switch stops its part of ATLAS for real. A bar across the top of every page says so, and
   the stopped buttons show a lock.
3. Select **Resume** and confirm. A drill needs no proof.

A drill is always marked as a drill. If a real problem happens during a drill, the drill becomes a
real incident and needs proof.

## Good to know

A correction to a published claim is still allowed while publishing is stopped.
