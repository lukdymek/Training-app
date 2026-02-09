from training.models import TrainingEmailLog

def has_received_assigned_email(training, person) -> bool:
    return TrainingEmailLog.objects.filter(
        training=training,
        person=person,
        template_type="ASSIGNED",
    ).exists()



def log_status_change_email_if_needed(training, person, new_status: str, sent_by, subject: str = "", body: str = "") -> bool:
    """
    Returns True if a STATUS_CHANGE log entry was created, False otherwise.
    Only logs if the person previously received an ASSIGNED email for this training.
    """
    if new_status not in ("REJECTED", "WITHDRAWN"):
        return False

    already_assigned = TrainingEmailLog.objects.filter(
        training=training,
        person=person,
        template_type="ASSIGNED",
    ).exists()

    if not already_assigned:
        return False

    TrainingEmailLog.objects.create(
        training=training,
        person=person,
        template_type="STATUS_CHANGE",
        subject=subject or f"Training update: {training.course_name}",
        body=body or f"(stub) Your status for {training.course_name} has changed to {new_status}.",
        status_at_send=new_status,
        sent_by=sent_by,
    )
    return True
