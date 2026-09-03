"""Plugin-manager bridge for atomic gateway conversation extensions."""

import logging

from gateway.conversation_extensions import (
    GatewayConversationExtension,
    GatewayRuntimeFacade,
    conversation_extension_registry,
    gateway_host_operations,
)

logger = logging.getLogger(__name__)


def register_plugin_conversation_extension(plugin_context, extension):
    """Register and track one generation-scoped extension bundle."""
    plugin_name = plugin_context.manifest.name
    if not isinstance(extension, GatewayConversationExtension):
        logger.warning(
            "Plugin '%s' tried to register a gateway conversation extension "
            "that is not a GatewayConversationExtension. Ignoring.",
            plugin_name,
        )
        return None

    manager = plugin_context._manager
    scope = manager.scope_key
    host = gateway_host_operations()
    previous_generation = conversation_extension_registry.active_generation(
        extension.extension_id, scope=scope
    )
    generation = conversation_extension_registry.reserve_generation(
        extension, scope=scope
    )
    facade = GatewayRuntimeFacade(
        extension_id=extension.extension_id,
        profile_name=plugin_context.profile_name,
        profile_home=scope,
        generation=generation,
        capabilities=extension.capabilities,
        host=host,
    )

    if extension.on_start is not None:
        try:
            extension.on_start(facade)
        except Exception:
            logger.warning(
                "Extension %s failed to start; rolling back registration",
                extension.extension_id,
                exc_info=True,
            )
            if host.cancel_tasks is not None:
                try:
                    host.cancel_tasks(extension.extension_id, scope, generation)
                except Exception:
                    logger.debug("rollback task cancellation failed", exc_info=True)
            return None

    if not conversation_extension_registry.publish(
        extension,
        generation=generation,
        scope=scope,
        expected_previous_generation=previous_generation,
    ):
        if extension.on_stop is not None:
            try:
                extension.on_stop(facade)
            except Exception:
                logger.debug("raced generation stop failed", exc_info=True)
        if host.cancel_tasks is not None:
            try:
                host.cancel_tasks(extension.extension_id, scope, generation)
            except Exception:
                logger.debug("raced generation cancellation failed", exc_info=True)
        logger.warning(
            "Extension %s predecessor changed before publish; discarded generation %s",
            extension.extension_id,
            generation,
        )
        return None

    if previous_generation is not None and host.cancel_tasks is not None:
        try:
            host.cancel_tasks(extension.extension_id, scope, previous_generation)
        except Exception:
            logger.warning(
                "Failed to cancel generation %s of extension %s after replacement",
                previous_generation,
                extension.extension_id,
                exc_info=True,
            )

    def release() -> None:
        if extension.on_stop is not None:
            try:
                extension.on_stop(facade)
            except Exception:
                logger.warning(
                    "Extension %s raised during stop; continuing teardown",
                    extension.extension_id,
                    exc_info=True,
                )
        if host.cancel_tasks is not None:
            try:
                host.cancel_tasks(extension.extension_id, scope, generation)
            except Exception:
                logger.debug(
                    "task cancellation failed during extension teardown",
                    exc_info=True,
                )
        conversation_extension_registry.unregister(
            extension.extension_id, generation=generation, scope=scope
        )

    handle = plugin_context._track(
        "gateway_conversation_extension",
        f"{extension.extension_id}@{generation}",
        release,
    )
    logger.info(
        "Plugin '%s' registered gateway conversation extension: %s "
        "(generation %s, capabilities: %s)",
        plugin_name,
        extension.extension_id,
        generation,
        ", ".join(sorted(extension.capabilities)) or "none",
    )
    return handle
