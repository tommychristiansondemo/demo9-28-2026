from aws_cdk import Stack, CfnOutput, Duration
from aws_cdk import aws_bedrockagentcore as bac
from constructs import Construct


class MemoryStack(Stack):
    """Bedrock AgentCore Memory: replaces the old client-resends-the-whole-
    history pattern with real server-side conversation state.

    Short-term memory (raw events, `list_events`) gives the agent the
    recent turns within one session without the browser having to resend
    them. A managed Summarization strategy, namespaced per actor (signed-in
    username) rather than per session, extracts a running cross-session
    summary in the background -- this is what lets a conversation stay
    coherent across weeks/months without replaying raw transcript forever,
    the exact production gap discussed with Tommy before he asked for this.

    Fully independent stack (no inbound dependency from AgentRuntime or
    Api) -- same cycle-avoidance pattern as PolicyPortal-ModelSelector:
    this stack only ever gets IMPORTED from (its memory id/ARN), never
    imports anything itself.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.memory = bac.Memory(
            self,
            "Memory",
            memory_name="PolicyPortalConversationMemory",
            description=(
                "Policy & Benefits Assistant conversation memory -- short-term "
                "session events plus a long-term per-user summary."
            ),
            expiration_duration=Duration.days(180),
            memory_strategies=[
                bac.ManagedMemoryStrategy(
                    bac.MemoryStrategyType.SUMMARIZATION,
                    strategy_name="ConversationSummary",
                    # {sessionId} is a MANDATORY part of a Summarization
                    # strategy's namespace -- confirmed live ("Validation
                    # failed during CreateMemory: ... requiring {sessionId}
                    # as a mandatory part of namespace"), a per-actor-only
                    # namespace (no {sessionId}) is rejected outright. Each
                    # session still gets its own summary record under this
                    # actor's prefix; cross-session recall is achieved at
                    # RETRIEVAL time instead, by searching the
                    # "/policyportal/{actor}/summary/" PREFIX (which matches
                    # every session's child namespace for that actor), not
                    # by trying to store one namespace per actor.
                    namespaces=["/policyportal/{actorId}/summary/{sessionId}/"],
                    description="Per-session summary, retrievable across all of one actor's sessions via a shared namespace prefix.",
                ),
            ],
        )

        CfnOutput(self, "MemoryId", value=self.memory.memory_id)
        CfnOutput(self, "MemoryArn", value=self.memory.memory_arn)
