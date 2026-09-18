from aws_cdk import Stack, CfnOutput
from aws_cdk import aws_bedrock as bedrock
from constructs import Construct


class GuardrailStack(Stack):
    """Real Bedrock Guardrail: PII masking + standard content filters. Wired
    into every Converse call the agent makes (see agents/policy_agent/agent.py)
    so PII entered by a user is anonymized in both directions, and unsafe
    content is filtered. This is the one enterprise feature not used by any
    of the three prior class demos in this series.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        pii_entities = [
            "US_SOCIAL_SECURITY_NUMBER",
            "EMAIL",
            "PHONE",
            "NAME",
            "ADDRESS",
            "CREDIT_DEBIT_CARD_NUMBER",
            "US_BANK_ACCOUNT_NUMBER",
            "AGE",
        ]

        content_filter_types = ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT", "PROMPT_ATTACK"]

        self.guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name="policy-portal-guardrail",
            description=(
                "Compliance guardrail for the Policy & Benefits Assistant: masks PII "
                "in both directions and filters unsafe content, per Section 9 of the "
                "Data Handling and Compliance Policy."
            ),
            blocked_input_messaging=(
                "Your message was blocked by content safety filters. Please rephrase "
                "your question about policies, leave, or benefits."
            ),
            blocked_outputs_messaging=(
                "I'm not able to provide that response due to content safety filters. "
                "Please try rephrasing your question."
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type=entity,
                        action="ANONYMIZE",
                    )
                    for entity in pii_entities
                ],
            ),
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=filter_type,
                        input_strength="MEDIUM",
                        # PROMPT_ATTACK is input-only; Bedrock rejects any output_strength other than NONE.
                        output_strength="NONE" if filter_type == "PROMPT_ATTACK" else "MEDIUM",
                    )
                    for filter_type in content_filter_types
                ],
            ),
        )

        CfnOutput(self, "GuardrailId", value=self.guardrail.attr_guardrail_id)
        CfnOutput(self, "GuardrailArn", value=self.guardrail.attr_guardrail_arn)
