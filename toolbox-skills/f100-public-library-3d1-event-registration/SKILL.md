---
name: f100-public-library-3d1-event-registration
description: Register patrons for library events and check event details. Use when patrons want to sign up for programs, classes, or activities.
argument-hint: The event name or ID and patron contact information.
---

# Library Event Registration

Use this skill to help patrons register for library events, check event availability, and confirm registration details. Always verify and confirm patron contact information before completing registration.

## Instructions

1. **Identify the patron**: Ask for their patron ID or account number if not provided.
2. **Get event details**: Ask for the event name or ID the patron wants to attend.
3. **Confirm patron contact information**: Verify the patron's email and phone number (if required for the event). Confirm this information before registration proceeds.
4. **Check event availability**: Use the event availability operation to see current capacity and event details. Note that results are synthetic workshop data.
5. **Complete registration**: If spaces are available, register the patron using the registration operation.
6. **Provide confirmation**: Share the event confirmation number, date, time, and any special instructions.
7. **Escalate immediately when**: Patron reports a child welfare concern, safety threat, or harassment during the interaction.
8. **Never**: Share attendance lists or registrations for other patrons, register someone under another person's account, or bypass age or eligibility requirements.

## Output format

- Confirm patron identity and event interest
- State event title, date, time, and duration
- Show registration confirmation number and status
- Provide any pre-event instructions or requirements
- For registration issues or conflicts: refer to library staff

## Boundaries

- Cannot access or share registrations for other patrons
- Cannot override age restrictions or capacity limits
- Cannot modify event details or schedules
- Cannot process group registrations without individual patron confirmations
- Child welfare concerns and patron safety incidents require immediate staff escalation
