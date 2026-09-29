# Deposit Rescue (internal: Project Safety Net)

Owner: Payments squad. Status: ready for release. Ticket: PAY-2143.

## Summary
When a card deposit is declined, players can finish the same deposit with Apple Pay or PayPal in one tap instead of starting over.

## Problem
In January, 35% of card deposit attempts failed, against 19% for Apple Pay and PayPal. After a decline, players see a generic "Payment failed" screen and must restart the deposit. Many never come back.

## Goals
- Recover declined card deposits without asking players to re-enter anything.
- Let players deposit up to $100 with Apple Pay and PayPal.

## How it works
1. The player starts a deposit and pays by card.
2. If the bank declines the card, a sheet opens with the message "Your bank declined this payment."
3. The sheet shows the same amount and a one-tap button: **Pay with Apple Pay** (iOS) or **Pay with PayPal** (iOS and Android).
4. The player confirms with Face ID, Touch ID or their PayPal login.
5. The money appears in the Blitz balance right away, like any other deposit.

## Rules and limits
- The rescue sheet appears once per declined card attempt.
- The amount stays the same and cannot be changed on the rescue sheet.
- New maximum per deposit: Apple Pay $100 (was $15) and PayPal $100 (was $40). The card maximum stays $100.
- The minimum deposit stays $5 for every method.
- Daily deposit limits set in Responsible Gaming settings still apply and take priority.
- Blitz charges no fees on deposits, whatever the method.

## Availability
- iOS and Android, app version 5.12 or later.
- All US states where Blitz cash games are available.
- Rollout: 10% of players on March 3, everyone on March 17.

## Error messages
| Situation | Message shown |
| --- | --- |
| Card declined | Your bank declined this payment. |
| Rescue payment also fails | We couldn't complete your deposit. No money was taken. |
| Daily limit reached | You've reached your daily deposit limit. |

## Edge cases
- If the player closes the sheet, nothing is charged and the card attempt stays declined.
- If the player has no card in Apple Pay, only PayPal is offered.
- A declined card attempt can show as a pending charge on the bank statement. The bank releases it, usually within 1 to 3 business days.

## Metrics
Card deposit success rate, share of declines rescued, deposits per paying player.

## Open questions
- Will Google Pay be added to the rescue sheet on Android? TBD
