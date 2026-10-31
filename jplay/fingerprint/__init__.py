"""The fingerprint service: your own FLAC collection as a Shazam for the house (Panako), as an API in Kubernetes.

  POST /api/recognize   a short WAV clip (the displays' microphone) -> the song, from your own files
  GET  /api/status      how far the index is, and when it last ran
  POST /api/index       look for new, changed and removed files now (nightly by itself)
  GET  /api/cover/{id}  the cover of a song (from the file, or cover.jpg / folder.jpg next to it)
"""
