"""Meeting invite reply generation functionality.

.. todo:: Expose ``generate_meeting_reply_content`` as an individual MCP tool
          (previously registered as the non-functional ``draft_meeting_reply_tool`` stub).
.. todo:: Expose ``process_meeting_invite_workflow`` as an individual MCP tool
          (previously registered as the non-functional ``process_invite_email_tool`` stub).
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from courier.errors import CourierError
from courier.models import EmailAddress
from courier.smtp_client import create_mime
from courier.workflows.calendar_mock import check_mock_availability
from courier.workflows.invite_parser import identify_meeting_invite_details

logger = logging.getLogger(__name__)


def generate_meeting_reply_content(
    invite_details: Dict[str, Any], availability_status: Dict[str, Any]
) -> Dict[str, Any]:
    """Generate meeting reply content based on invite details and availability.

    Args:
        invite_details: Dictionary with meeting invite details (from invite_parser)
        availability_status: Dictionary with availability details (from calendar_mock)

    Returns:
        Dictionary with reply details:
            - reply_subject: Subject line for the reply
            - reply_body: Body text for the reply
            - reply_type: "accept" or "decline"
    """
    # Validate input
    if not isinstance(invite_details, dict) or not isinstance(
        availability_status, dict
    ):
        logger.error(
            f"Invalid input types: invite_details={type(invite_details)}, availability_status={type(availability_status)}"
        )
        return {
            "reply_subject": "Error: Invalid Meeting Invite",
            "reply_body": "Could not process the meeting invite due to invalid data.",
            "reply_type": "error",
        }

    # Extract key details
    subject = invite_details.get("subject", "Meeting")
    start_time = invite_details.get("start_time")
    end_time = invite_details.get("end_time")
    organizer = invite_details.get("organizer", "Meeting Organizer")
    location = invite_details.get("location", "Not specified")

    # Format date/time for display
    formatted_time = _format_meeting_time(start_time, end_time)

    # Check if available
    is_available = availability_status.get("available", False)
    decline_reason = (
        availability_status.get("reason", "Schedule conflict")
        if not is_available
        else ""
    )

    # Generate reply based on availability
    if is_available:
        return _generate_accept_reply(subject, formatted_time, organizer, location)
    else:
        return _generate_decline_reply(
            subject, formatted_time, organizer, location, decline_reason
        )


def _format_meeting_time(
    start_time: Optional[datetime], end_time: Optional[datetime]
) -> str:
    """Format meeting time for display in reply.

    Args:
        start_time: Meeting start time
        end_time: Meeting end time

    Returns:
        Formatted time string
    """
    if not start_time:
        return "scheduled time"

    # Format just the start time if no end time
    if not end_time:
        return start_time.strftime("%A, %B %d, %Y at %I:%M %p")

    # Check if same day
    same_day = start_time.date() == end_time.date()

    if same_day:
        # Format as "Monday, January 1, 2025 from 10:00 AM to 11:00 AM"
        return (
            f"{start_time.strftime('%A, %B %d, %Y')} from "
            f"{start_time.strftime('%I:%M %p')} to {end_time.strftime('%I:%M %p')}"
        )
    else:
        # Format as "Monday, January 1, 2025 at 10:00 AM to Tuesday, January 2, 2025 at 11:00 AM"
        return (
            f"{start_time.strftime('%A, %B %d, %Y')} at {start_time.strftime('%I:%M %p')} to "
            f"{end_time.strftime('%A, %B %d, %Y')} at {end_time.strftime('%I:%M %p')}"
        )


def _generate_accept_reply(
    subject: str, formatted_time: str, organizer: str, location: str
) -> Dict[str, Any]:
    """Generate reply content for accepting a meeting invite.

    Args:
        subject: Meeting subject
        formatted_time: Formatted meeting time string
        organizer: Meeting organizer
        location: Meeting location

    Returns:
        Dictionary with reply details
    """
    reply_subject = f"Accepted: {subject}"

    reply_body = (
        f'I\'ll attend the meeting: "{subject}" on {formatted_time}.\n\n'
        f"Location: {location}\n"
        "\n"
        "Thank you for the invitation.\n"
        "\n"
        "Best regards,"
    )

    logger.debug(f"Generated accept reply for meeting: {subject}")

    return {
        "reply_subject": reply_subject,
        "reply_body": reply_body,
        "reply_type": "accept",
    }


def _generate_decline_reply(
    subject: str, formatted_time: str, organizer: str, location: str, reason: str
) -> Dict[str, Any]:
    """Generate reply content for declining a meeting invite.

    Args:
        subject: Meeting subject
        formatted_time: Formatted meeting time string
        organizer: Meeting organizer
        location: Meeting location
        reason: Reason for declining

    Returns:
        Dictionary with reply details
    """
    reply_subject = f"Declined: {subject}"

    reply_body = (
        f'I\'m unable to attend the meeting: "{subject}" on {formatted_time}.\n\n'
        f"Reason: {reason}\n"
        "\n"
        "Thank you for the invitation. Please let me know if there's an alternative time "
        "that might work or if I can contribute in another way.\n"
        "\n"
        "Best regards,"
    )

    logger.debug(f"Generated decline reply for meeting: {subject}")

    return {
        "reply_subject": reply_subject,
        "reply_body": reply_body,
        "reply_type": "decline",
    }


def process_meeting_invite_workflow(
    client: Any,
    folder: str,
    uid: int,
    availability_mode: str = "random",
) -> Dict[str, Any]:
    """Fetch an email, identify a meeting invite, check availability, and save a draft reply.

    Args:
        client: An ``ImapClient`` instance (duck-typed to avoid circular import).
        folder: IMAP folder containing the email.
        uid: UID of the email.
        availability_mode: One of random, always_available, always_busy,
                           business_hours, weekdays.

    Returns:
        Dict with keys ``status``, ``message``, ``draft_uid``,
        ``draft_folder``, ``availability``.
    """
    result: Dict[str, Any] = {
        "status": "error",
        "message": "An error occurred during processing",
        "draft_uid": None,
        "draft_folder": None,
        "availability": None,
    }

    try:
        email_obj = client.fetch_email(uid, folder)
        if not email_obj:
            result["message"] = f"Email with UID {uid} not found in folder {folder}"
            return result

        invite_result = identify_meeting_invite_details(email_obj)
        if not invite_result["is_invite"]:
            result["status"] = "not_invite"
            result["message"] = "The email is not a meeting invite"
            return result

        invite_details = invite_result["details"]

        avail_result = check_mock_availability(
            invite_details.get("start_time"),
            invite_details.get("end_time"),
            availability_mode,
        )
        result["availability"] = avail_result["available"]

        reply_content = generate_meeting_reply_content(invite_details, avail_result)

        reply_from = (
            email_obj.to[0]
            if email_obj.to
            else EmailAddress(name="Me", address=client.config.username)
        )

        mime_message = create_mime(
            original_email=email_obj,
            from_addr=reply_from,
            body=reply_content["reply_body"],
            subject=reply_content["reply_subject"],
            reply_all=False,
        )

        # ponytail: CourierError shim keeps today's failure text; C2 owns
        # the redesign.
        try:
            draft_uid = client.save_draft_mime(mime_message).uid
        except CourierError:
            draft_uid = None
        if draft_uid:
            drafts_folder = client.resolve_drafts_folder()
            result["status"] = "success"
            result["message"] = f"Draft reply created: {reply_content['reply_type']}"
            result["draft_uid"] = draft_uid
            result["draft_folder"] = drafts_folder
        else:
            result["message"] = "Failed to save draft"

    except Exception as e:
        logger.error(f"Error processing meeting invite: {e}")
        result["message"] = f"Error: {e}"

    return result
