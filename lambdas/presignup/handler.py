def handler(event, context):
    """Zero-friction onboarding for a training-class demo: auto-confirm every
    self-signup so students can start using the app immediately, no email
    verification step.
    """
    event["response"]["autoConfirmUser"] = True
    if event.get("request", {}).get("userAttributes", {}).get("email"):
        event["response"]["autoVerifyEmail"] = True
    return event
