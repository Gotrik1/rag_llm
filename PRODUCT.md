<!--
Last updated: 2026-09-09
Source of truth: product-owner requirements captured in the active Codex task.
-->

# AI ИАС Энергобаланс — product context

## Platform

Web application for desktop and mobile browsers, deployed as an HTTPS site on a public IP and port.

## Users

- Registered employees who ask questions across corporate documents.
- Project owners who keep private project documents, chat history and project memory.
- Administrators who maintain the shared document library.

## Core purpose

Provide production-grade grounded answers over documents without placing whole documents in the system prompt. Answers must preserve formulas and link to a human-readable document source with a useful locator such as a section or page.

## Product rules

- Existing documents form the shared library and are readable by every authenticated user.
- Every user's projects, chats, memory and uploaded project documents are private to that user.
- Each project chooses one knowledge scope: project documents only, or project documents plus the shared library.
- Page refresh and temporary disconnects must not lose the authenticated session, chat history or an in-progress durable generation.
- Several chats can run independently at the same time.
- Document ingestion status has one source of truth; queued, processing, ready and failed states must agree across the interface.
- A source is a document and a human-readable locator, never an internal chunk identifier.
- The interface must show which shared and private documents are available, when they were uploaded and whether a newer version exists.

## Experience principles

- Make trust inspectable: show documents, locators, formulas, processing state and warnings near the answer.
- Keep advanced diagnostics available without making them necessary for ordinary work.
- Prefer secure, persistent server-side sessions and least-privilege data access.
- Preserve the established dark navigation, light workspace and compact technical panel visual language.
