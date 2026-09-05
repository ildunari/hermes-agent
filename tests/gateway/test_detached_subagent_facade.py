import dataclasses
import pytest
from gateway.conversation_extensions import (GatewayHostOperations, GatewayRuntimeFacade,
    SubagentSessionRequest, AuthenticatedDmRequest, CapabilityDenied)
from agent.subagent_lifecycle_detached import SubagentSessionBinding, SubagentSessionIdentity


def test_facade_gates_host_resolution_and_allows_host_authenticated_multiplex():
    request = SubagentSessionRequest("guest-key", "saved", "guest", "contact", "guest")
    binding = SubagentSessionBinding(SubagentSessionIdentity("/profiles/guest", "saved", "contact", "guest"), "token")
    facade = GatewayRuntimeFacade(extension_id="owner", profile_name="poke", profile_home="/profiles/poke",
        generation=1, capabilities=frozenset({"detached_subagents"}),
        host=GatewayHostOperations(bind_subagent_session=lambda r: binding))
    assert facade.bind_subagent_session(request) == binding
    with pytest.raises(CapabilityDenied):
        facade.bind_subagent_session(dataclasses.replace(request, principal="other"))
    facade._capabilities = frozenset()
    with pytest.raises(CapabilityDenied):
        facade.bind_subagent_session(request)


def test_attachments_are_bounded_local_paths_and_text_remains_required():
    request = AuthenticatedDmRequest("test", "existing", "caption", "reservation", attachments=("/retained/image.png",))
    assert request.attachments == ("/retained/image.png",)
    for attachments in (["/x"], ("https://example.com/file",), ("relative",), ("/x",) * 11, ("/bad\0",)):
        with pytest.raises(ValueError):
            dataclasses.replace(request, attachments=attachments)
    with pytest.raises(ValueError):
        dataclasses.replace(request, text="")
