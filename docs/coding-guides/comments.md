# Code Comment Guidelines

## Purpose

Keep the codebase concise, readable, and low-noise by using comments only when they preserve information that cannot be reasonably understood from the code itself.

Comments are high-value contextual information, not narration or documentation of obvious implementation details.

## Core Principle

**Code should speak for itself.** A well-written function should be understandable through its names, structure, types, abstractions, and control flow.

Comments should primarily explain:

- Why a non-obvious decision was made
- External constraints or system behavior
- Business or architectural context
- Important invariants or assumptions
- Compatibility requirements
- Non-obvious tradeoffs
- Temporary workarounds and why they exist

Comments should generally **not** explain:

- What the code obviously does
- How a loop works
- What a variable contains
- What a function call does
- The sequence of straightforward operations
- Information already apparent from names and structure
- Line-by-line implementation details

## Before Writing a Comment

Ask:

1. Is this information obvious from the code?
2. Could the code be made clearer through better naming or structure instead?
3. Does this explain *why*, rather than *what*/*how*?
4. Is this information important enough that a future engineer needs to know it?
5. Will the comment remain useful if the implementation is refactored?

If the information is already apparent from the code, do not add the comment.

## Prefer Better Code Over More Comments

If code requires a large comment to explain what it does, improve the code first. Prefer:

- Better variable and function names
- Smaller functions
- Clearer abstractions
- Explicit types
- Straightforward control flow
- Well-defined boundaries

Do not write a long comment to compensate for unclear code.

## Comment Size

Keep comments short and information-dense. Avoid large explanatory blocks, multi-paragraph comments, and comments that walk through an implementation step by step.

A comment should contain the minimum context necessary. If explaining something requires a large amount of prose, reconsider whether the information belongs in:

- The code itself
- A docstring describing a public contract
- Architectural documentation (see [`architecture.md`](architecture.md))
- A design document
- An issue or ADR

Do not turn ordinary source files into essays.

## Examples

### Do not narrate obvious code

Bad:

```python
# Loop through all files
for file in files:
    # Check if the file exists
    if file.exists():
        # Process the file
        process(file)
```

Good:

```python
# Files can disappear between discovery and ingestion because filesystem
# changes happen concurrently with the watcher.
if file.exists():
    process(file)
```

The second comment contains context that cannot be inferred from the code.

### Do not explain variable assignments

Bad:

```python
# Create an empty dictionary to store users by ID
users_by_id = {}
```

Good:

```python
users_by_id = {}
```

### Do not explain straightforward control flow

Bad:

```python
# Return early if the request is invalid
if not request.is_valid():
    return None
```

Good:

```python
if not request.is_valid():
    return None
```

### Explain non-obvious decisions

```python
# Hash only after the metadata fast-path; hashing large files is expensive.
if metadata_changed:
    content_hash = hash_file(path)
```

### Explain external constraints

```python
# The provider returns HTTP 200 for application-level failures.
status = response.json()["status"]
```

### Explain intentional deviations

```python
# Keep this serialized because the upstream API applies a shared rate limit.
```

## Do Not Add Comments During Routine Refactoring

When modifying existing code:

- Do not add comments merely because you touched a section.
- Do not add comments describing the changes you made.
- Do not preserve stale comments simply because they already exist.
- Remove comments that merely describe code that is now self-explanatory.
- Update contextual comments when the underlying decision or constraint changes.

A refactor should generally make code clearer without increasing comment volume.

## Comment Quality

Comments should add information that an experienced engineer could not easily derive from the code.

Prefer *"Why is this necessary?"* over *"What is happening here?"*
Prefer *"What constraint caused this design?"* over *"What does this function do?"*
Prefer *"What would break if this changed?"* over *"What does this line accomplish?"*

## File-Level Comments

Use file-level documentation only when the file has important context that cannot reasonably be derived from its contents. Good examples:

- Architectural purpose
- Integration boundary
- Important module-level invariant
- External system constraint
- Reason for an unusual design

Do not use file comments to provide a tour of the functions or explain the structure of the file.

## Docstrings

Use docstrings when they communicate a meaningful public contract, such as:

- Important behavior
- Parameters that have non-obvious semantics
- Return-value guarantees
- Exceptions
- Side effects
- Public API expectations

Do not automatically generate verbose docstrings for every function. Private, obvious helpers generally do not need explanatory docstrings.

## Strong Default

When deciding between adding a comment and improving the code: **improve the code.**

When deciding whether an existing comment is necessary: **delete it if the code already communicates the same information.**

When a comment is necessary: **make it as short as possible while preserving the important context.**

## Anti-Bloat Rule

Actively resist comment bloat. Do not produce comments simply because:

- A section of code looks complex
- A function is long
- A loop exists
- A variable is introduced
- An operation is performed
- A reviewer might expect commentary
- You want to demonstrate understanding
- Your reasoning while writing the code produced an explanation worth writing down

Internal reasoning is not, on its own, a reason to put that reasoning into the source code.

## Final Check

Before completing a code change, review every newly added comment and ask: *"Does this preserve context that would otherwise be lost?"* If not, remove it.

The desired codebase has few comments, but highly valuable ones. The objective is not maximum documentation - it's maximum signal-to-noise ratio for engineers reading the code.
