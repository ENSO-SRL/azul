"""Atlas membership amounts in cents; AZUL Amount already includes ITBIS."""
import os

# RD$500 service + RD$90 ITBIS = RD$590 charged to the card.
# Keep the existing environment contract: MEMBERSHIP_AMOUNT is the TOTAL.
MEMBERSHIP_AMOUNT = int(os.getenv("MEMBERSHIP_AMOUNT", "59000"))
MEMBERSHIP_ITBIS = int(os.getenv("MEMBERSHIP_ITBIS", "9000"))
