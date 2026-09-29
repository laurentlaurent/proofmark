# PRD: Smart Limits (v2)

**Owner:** Responsible Gaming squad · **Code name:** Project ANCHOR · **Epic:** RG-733
**Feature flag:** `rg.smart_limits_enabled` · **Target release:** app version 4.21

## Summary

Deposit Limits become Smart Limits. Besides deposits, players can now limit how much time they spend playing and how much they can lose in a day. The app also suggests a limit to new players during sign-up, and warns players when they get close to any limit.

## Who gets it

- All players in the US. Players in New Jersey get it first because of the state's new rules (NJ DGE directive, effective March 1).
- iOS and Android at launch. Web follows in app version 4.23.
- Rollout: New Jersey on day one, all other states 14 days later.

## How it works

1. Player opens **Settings > Responsible Gaming** and taps **Smart Limits** (replaces **Deposit Limit**).
2. Player picks a limit type: **Deposit**, **Loss** or **Play time**.
3. Player picks a period: daily, weekly or monthly. Play time is daily only.
4. Player enters an amount (or a number of hours) and taps **Set limit**.
5. The app shows a summary: "Your new limit starts now. You can lower it any time."

## New player suggestion

- During sign-up, after identity verification, new players see the screen "Want to set a limit?" with a suggested deposit limit of $250 per week.
- Players can accept, change the amount, or tap **Not now**.
- Players who tap **Not now** are asked again after 30 days. (TBD: is this a push notification or an in-app banner?)

## Limits and rules

- Deposit limit: $10 to $10,000 per period.
- Loss limit: $10 to $5,000 per period. Loss = deposits minus withdrawals and winnings in the period.
- Play time: 1 to 12 hours per day.
- Lowering any limit takes effect right away.
- Raising any limit now takes effect after **72 hours** (was 24 hours). Removing a limit also takes 72 hours.
- Limits still apply to every payment method.
- Players can still pause their account for 7, 30 or 90 days. A new option adds a 6-month pause.

## Warnings

- At 80% of any limit, the player sees a banner: "You've used 80% of your weekly deposit limit."
- At 100%, deposits (or entering new games, for loss and play-time limits) are blocked until the period resets. Message: "You've reached your limit. It resets on {{reset_date}}."
- Games already in progress are allowed to finish when a play-time limit is reached.

## Support notes

- Agents cannot raise or remove a limit for a player. They can only lower one on the player's request.
- If a player says a limit was applied wrongly, escalate to the RG team via [link].

## Implementation notes (internal)

- Loss is computed by the new `rg-ledger-aggregator` service, recalculated every 15 minutes, so the 80% banner can lag behind.
- Session timer uses the heartbeat endpoint; see RG-751 for idempotency fixes.
- Suggested amount comes from a static config for now; ML-based suggestion is behind flag `rg.smart_suggest_ml` (not in this release).

## Not decided yet

- Whether bonus funds count toward the loss limit.
- Which time zone the daily reset uses (player's local time or Eastern Time).
- Whether existing deposit limits are migrated automatically or players must set them again.
