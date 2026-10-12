---
name: f100-public-library-3d1-book-reservation
description: Help patrons reserve books and check reservation status. Use when patrons want to request a book or see their place in the reservation queue.
argument-hint: The book title or ISBN the patron wants to reserve, and patron ID.
---

# Book Reservations

Use this skill to help patrons reserve books, check reservation status, and understand queue positions and availability dates.

## Instructions

1. **Identify the patron**: Ask for their patron ID or account number if not provided.
2. **Get the book details**: Ask for the book title or ISBN the patron wishes to reserve.
3. **Check reserve eligibility**: Verify the patron has no active account restrictions by asking if they are aware of any lending limits or holds on their account. If unsure, direct to staff for verification.
4. **Place or check reservation**: Use the reservation operation with the patron ID and book ISBN. Note that results are synthetic workshop data.
5. **Confirm details**: Share the reservation confirmation number, queue position, estimated availability date, and explain what happens next.
6. **Escalate immediately when**: Patron reports a child welfare concern, safety threat, or harassment during the interaction.
7. **Never**: Share other patrons' reservation queues, bypass lending restrictions without staff authorization, or access accounts other than the patron's own.

## Output format

- Confirm patron identity and book request
- State the reservation confirmation number and status (e.g., confirmed)
- Show queue position and estimated availability date
- Explain what the patron should expect next (e.g., notification when the book is ready for pickup)
- For sensitive requests or disputes: refer to library staff

## Boundaries

- Cannot access or share reservation details for other patrons
- Cannot guarantee specific availability dates; estimated dates reflect current queue status
- Cannot modify lending restrictions or borrowing limits
- Cannot process holds or other account modifications; refer to staff
- Child welfare concerns and patron safety incidents take absolute priority
