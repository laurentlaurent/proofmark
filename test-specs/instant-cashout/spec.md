# PRD: Instant Cash Out (v2)

**Owner:** Payments squad · **Code name:** Project LIGHTNING · **Epic:** PAY-2291
**Feature flag:** `payments.instant_cashout_v2` · **Target release:** app version 4.18

## Summary

Players can now withdraw their winnings to an eligible debit card in minutes instead of waiting for a bank transfer. Instant Cash Out replaces the old "Fast Withdrawal" option.

## Who gets it

- Players in the US who have completed identity verification (KYC level 2).
- iOS and Android. Not available on the web app at launch.
- Rollout: 10% of eligible players on day one, 100% after two weeks if the error rate stays under 1%.

## How it works

1. Player opens **Wallet** and taps **Cash Out**.
2. Player enters an amount and picks **Instant (to debit card)** as the method.
3. The app shows the fee and the amount the player will receive before they confirm.
4. Player taps **Confirm Cash Out**.
5. Funds usually arrive within 30 minutes. The player gets a push notification: "Your cash out is on its way!"

## Limits and fees

- Minimum instant cash out: $10.
- Maximum instant cash out: $2,500 per transaction and $5,000 per day.
- Fee: 1.5% of the amount, minimum $0.50.
- Standard bank transfer (ACH) stays free and takes 3 to 5 business days.

## Error states

- Card not eligible: "This card can't receive instant cash outs. Try another debit card or use bank transfer."
- Daily limit reached: "You've reached today's instant cash out limit. Try again tomorrow or use bank transfer."
- If the card network is down, the cash out is automatically retried for up to 24 hours, then refunded to the player's Blitz balance.

## Implementation notes (internal)

- New PSP integration via the Tabapay push-to-card API; BIN lookup decides eligibility.
- Webhook `cashout.settled` updates the ledger; see PAY-2304 for the retry job.
- A/B test: fee display before vs after amount entry (owner: growth squad).

## Not decided yet

- Whether prepaid debit cards are supported.
