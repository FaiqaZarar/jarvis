"""J-01 acceptance check: the empty skeleton imports and runs without error."""


def test_all_modules_import():
    import jarvis_server.internal.db
    import jarvis_server.internal.normalizer.hashing
    import jarvis_server.internal.normalizer.normalize
    import jarvis_server.internal.normalizer.converters.base
    import jarvis_server.internal.normalizer.converters.txt
    import jarvis_server.internal.normalizer.converters.docx
    import jarvis_server.internal.normalizer.converters.pdf
    import jarvis_server.internal.file_handler.reader
    import jarvis_server.internal.chunk.chunk
    import jarvis_server.internal.embed.embed
    import jarvis_server.organize.clustering
    import jarvis_server.organize.reconcile
    import jarvis_server.mcp_tools.search
    import jarvis_server.mcp_tools.hierarchy
    import jarvis_server.connectors.filesystem
    import jarvis_server.connectors.drive
    import jarvis_server.connectors.notion
