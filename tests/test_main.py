import sys
import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import main
from authentication import AUTH_TOKEN
from fastapi.testclient import TestClient

class TestUpdateYtDlp(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Setup TestClient and ensure AUTH_TOKEN matches
        self.client = TestClient(main.app)
        self.headers = {"authorization": "Bearer " + AUTH_TOKEN}

    @patch('main.os._exit')
    @patch('main.asyncio.create_subprocess_exec')
    async def test_update_ytdlp_success(self, mock_create_subprocess, mock_exit):
        # Mock successful subprocess execution
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"Successfully installed yt-dlp-2024.03.10", b""))
        mock_process.returncode = 0
        mock_create_subprocess.return_value = mock_process

        response = self.client.post("/api/update-ytdlp", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertIn("Successfully installed", response.json()["output"])
        self.assertTrue(response.json()["require_restart"])

        # In test we won't wait for the background tasks to fire fully here normally,
        # but to avoid it actually exiting python when testing if it does run, we mock os._exit.

    @patch('main.asyncio.create_subprocess_exec')
    async def test_update_ytdlp_already_satisfied(self, mock_create_subprocess):
        # Mock already satisfied subprocess execution
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"Requirement already satisfied", b""))
        mock_process.returncode = 0
        mock_create_subprocess.return_value = mock_process

        response = self.client.post("/api/update-ytdlp", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertIn("Requirement already satisfied", response.json()["output"])
        self.assertFalse(response.json()["require_restart"])

    @patch('main.asyncio.create_subprocess_exec')
    async def test_update_ytdlp_failure(self, mock_create_subprocess):
        # Mock failed subprocess execution
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"Error installing package"))
        mock_process.returncode = 1
        mock_create_subprocess.return_value = mock_process

        response = self.client.post("/api/update-ytdlp", headers=self.headers)

        self.assertEqual(response.status_code, 500)
        self.assertIn("Error installing package", response.json()["detail"])

if __name__ == '__main__':
    unittest.main()
