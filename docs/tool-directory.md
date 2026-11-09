> Schema inventory captured October 2026. All 65 allowlisted upstream tools are now published directly through Gemini Live. The former Docs replacement helper and tool-search/help mechanisms described below have been removed.

# Luna service tool inventory

Reviewed from the connected Spotify and Google Workspace servers on October 2, 2026.

The current configuration permits 65 upstream tools: six Spotify tools and 59 Google tools.
Luna also supplies the Docs `replace_text` helper, for 66 service entries. Gmail supports drafts, not sending email.
Discovery does not establish API permission. The live Google profile check returned
`integration_unavailable`; prior project verification reported a Workspace Developer Preview
enrollment requirement. The current failure reason still needs confirmation.

This is a reference inventory, not a system prompt. The runtime discovers the
configured, allowlisted tools and publishes only selected schemas to Luna.
`search_tools` enables additional tools and `get_tool_help` supplies operation guidance.

## Google Calendar (9)

| Tool | Purpose |
| --- | --- |
| `google_calendar__create_event` | Create a calendar event. |
| `google_calendar__delete_event` | Delete a calendar event. |
| `google_calendar__get_event` | Read one calendar event. |
| `google_calendar__list_calendars` | List calendars and resolve calendar names to IDs. |
| `google_calendar__list_events` | List calendar events with explicit time or other filters. |
| `google_calendar__respond_to_event` | Accept, decline or tentatively accept an invitation. |
| `google_calendar__search_events` | Search your primary calendar by topic or meaning. |
| `google_calendar__suggest_time` | Find meeting times across calendars. |
| `google_calendar__update_event` | Change a calendar event. |

## Google Chat (4)

| Tool | Purpose |
| --- | --- |
| `google_chat__list_messages` | Read messages in a known conversation or thread. |
| `google_chat__search_conversations` | Find conversations by name or participants, not message contents. |
| `google_chat__search_messages` | Search message contents across accessible conversations. |
| `google_chat__send_message` | Send a message to a Google Chat conversation. |

## Google Docs (3)

| Tool | Purpose |
| --- | --- |
| `google_docs__read_doc` | Read document text and structure, optionally with comments. |
| `google_docs__replace_text` | Replace matching document text; verify with read_doc. |
| `google_docs__update_doc` | Edit document content or formatting with batch requests. |

## Google Drive files (8)

| Tool | Purpose |
| --- | --- |
| `google_drive__copy_file` | Copy an existing Drive file. |
| `google_drive__create_file` | Create or upload a Drive file. |
| `google_drive__download_file_content` | Download file content as base64. |
| `google_drive__get_file_metadata` | Read file metadata. |
| `google_drive__get_file_permissions` | List file access permissions. |
| `google_drive__list_recent_files` | List recent Drive files. |
| `google_drive__read_file_content` | Read file content as natural language, optionally with comments. |
| `google_drive__search_files` | Find Drive files using a structured query. |

## Gmail messages and drafts (23)

| Tool | Purpose |
| --- | --- |
| `google_gmail__apply_sensitive_message_label` | Mark one email as trash or spam; prefer dedicated tools. |
| `google_gmail__apply_sensitive_thread_label` | Mark a conversation as trash or spam; prefer dedicated tools. |
| `google_gmail__create_draft` | Create an email draft or reply draft; does not send. |
| `google_gmail__create_label` | Create an email label, including nested labels. |
| `google_gmail__get_draft` | Read one draft. |
| `google_gmail__get_message` | Read one email, excluding drafts. |
| `google_gmail__get_thread` | Read an email conversation, excluding drafts. |
| `google_gmail__label_message` | Add labels to one email. |
| `google_gmail__label_thread` | Add labels to an entire email conversation. |
| `google_gmail__list_drafts` | Search or list email drafts. |
| `google_gmail__list_labels` | List email labels and their IDs. |
| `google_gmail__mark_message_spam` | Mark one email as spam. |
| `google_gmail__mark_thread_spam` | Mark an email conversation as spam. |
| `google_gmail__search_threads` | Search email conversations; returns snippets. |
| `google_gmail__trash_message` | Move one email or draft message to trash. |
| `google_gmail__trash_thread` | Move an email conversation to trash. |
| `google_gmail__unlabel_message` | Remove labels from one email. |
| `google_gmail__unlabel_thread` | Remove labels from an email conversation. |
| `google_gmail__unmark_message_spam` | Remove the spam mark from one email. |
| `google_gmail__unmark_thread_spam` | Remove the spam mark from a conversation. |
| `google_gmail__untrash_message` | Restore one email from trash. |
| `google_gmail__untrash_thread` | Restore a conversation from trash. |
| `google_gmail__update_message_labels` | Add and remove labels on one email atomically. |

## Google contacts and profile (3)

| Tool | Purpose |
| --- | --- |
| `google_people__get_user_profile` | Read your own name and email. |
| `google_people__search_contacts` | Find personal contacts; confirm identity before using results. |
| `google_people__search_directory_people` | Find Workspace directory people; confirm identity before using results. |

## Google Sheets (6)

| Tool | Purpose |
| --- | --- |
| `google_sheets__get_spreadsheet` | Read spreadsheet structure and optionally grid data or comments. |
| `google_sheets__get_values` | Read cell values in a spreadsheet range. |
| `google_sheets__insert_dimension` | Insert rows or columns. |
| `google_sheets__update_formulas` | Write formulas to a range. |
| `google_sheets__update_spreadsheet` | Edit spreadsheet structure or formatting with batch requests. |
| `google_sheets__update_values` | Write cell values to a range. |

## Google Slides (4)

| Tool | Purpose |
| --- | --- |
| `google_slides__read_presentation` | Read a presentation's slides and structure. |
| `google_slides__read_slide_page` | Read one slide's content and structure. |
| `google_slides__read_slide_page_thumbnail` | Get a slide thumbnail URL and dimensions. |
| `google_slides__update_presentation` | Edit slides, content or formatting with batch requests. |

## Spotify music, playback, and playlists (6)

| Tool | Purpose |
| --- | --- |
| `spotify__getAvailableDevices` | List Spotify Connect devices. |
| `spotify__getMyPlaylists` | List your playlists. |
| `spotify__getNowPlaying` | Read current playback, device and volume. |
| `spotify__pausePlayback` | Pause playback. |
| `spotify__playMusic` | Start a track, album, artist or playlist. |
| `spotify__searchSpotify` | Search music, playlists and podcasts. |

## Additional Spotify server tools

These 24 tools were also discovered and reviewed. They remain excluded by Luna's current Spotify allowlist.
They are not advertised or executable unless that configuration changes.

| Tool | Purpose |
| --- | --- |
| `spotify__getPlaylistTracks` | List tracks in a playlist. |
| `spotify__getRecentlyPlayed` | List recently played tracks. |
| `spotify__getUsersSavedTracks` | List your liked songs. |
| `spotify__removeUsersSavedTracks` | Remove tracks from liked songs. |
| `spotify__getQueue` | Read current playback and queued items. |
| `spotify__getTopTracks` | List your most-played tracks. |
| `spotify__getTopArtists` | List your most-played artists. |
| `spotify__skipToNext` | Skip to the next queued track. |
| `spotify__skipToPrevious` | Skip to the previous track. |
| `spotify__createPlaylist` | Create a playlist. |
| `spotify__addTracksToPlaylist` | Add tracks or podcast episodes to a playlist. |
| `spotify__resumePlayback` | Resume playback. |
| `spotify__addToQueue` | Add music to the playback queue. |
| `spotify__setVolume` | Set playback volume; requires Premium. |
| `spotify__adjustVolume` | Raise or lower volume; requires Premium. |
| `spotify__getAlbums` | Read album details. |
| `spotify__getAlbumTracks` | List tracks in an album. |
| `spotify__saveOrRemoveAlbumForUser` | Save or remove albums from your library. |
| `spotify__checkUsersSavedAlbums` | Check whether albums are saved. |
| `spotify__getPlaylist` | Read playlist details. |
| `spotify__updatePlaylist` | Change playlist name, description or visibility. |
| `spotify__removeTracksFromPlaylist` | Remove tracks from a playlist. |
| `spotify__reorderPlaylistItems` | Move tracks within a playlist. |
| `spotify__unfollowPlaylist` | Remove a playlist from your library; does not delete it. |

## Runtime controls

`search_tools`, `get_tool_help`, `connect_integration`, and `authorize_integration`
are always available when integrations are configured, alongside selected service schemas.
Pipecat supplies `cancel_authorize_integration` for explicit cancellation.
