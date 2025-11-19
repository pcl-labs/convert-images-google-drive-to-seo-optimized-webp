from src.workers.core.youtube_api import fetch_video_metadata


class FakeYouTubeClient:
    def __init__(self, payload):
        self.payload = payload

    def fetch_video(self, video_id: str):
        return self.payload


def test_fetch_video_metadata_extracts_chapters():
    payload = {
        "items": [
            {
                "status": {"privacyStatus": "public"},
                "snippet": {
                    "title": "Demo",
                    "description": "00:00 Intro\n01:30 Deep dive\n02:05 Wrap up",
                    "channelTitle": "Channel",
                    "channelId": "chan-1",
                    "publishedAt": "2024-01-01T00:00:00Z",
                    "thumbnails": {},
                    "categoryId": "22",
                    "tags": ["demo"],
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT3M20S"},
            }
        ]
    }
    client = FakeYouTubeClient(payload)
    result = fetch_video_metadata(client, "abc123")
    metadata = result["metadata"]
    chapters = metadata.get("chapters")
    assert isinstance(chapters, list)
    assert len(chapters) == 3
    assert chapters[0]["title"] == "Intro"
    assert chapters[0]["start_seconds"] == 0
    assert chapters[1]["start_seconds"] == 90


def test_fetch_video_metadata_handles_missing_chapters():
    payload = {
        "items": [
            {
                "status": {"privacyStatus": "public"},
                "snippet": {
                    "title": "Demo",
                    "description": "Check out 2024 recap at 24:00",
                    "channelTitle": "Channel",
                    "channelId": "chan-1",
                    "publishedAt": "2024-01-01T00:00:00Z",
                    "thumbnails": {},
                    "categoryId": "22",
                    "tags": ["demo"],
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT3M20S"},
            }
        ]
    }
    client = FakeYouTubeClient(payload)
    result = fetch_video_metadata(client, "abc123")
    metadata = result["metadata"]
    assert "chapters" not in metadata
