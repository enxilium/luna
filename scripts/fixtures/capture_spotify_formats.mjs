// Capture actual MCP envelopes with synthetic HTTP; never use real credentials or services.
// Run with Luna's Node runtime: node scripts/fixtures/capture_spotify_formats.mjs [server-directory]
import test from "node:test";
import fs from "node:fs";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
const root =
  process.argv[2] ??
  path.join(os.homedir(), "Library/Application Support/Luna/servers/spotify");
const { connect, mockConfig, mockHttp } = await import(
  pathToFileURL(path.join(root, "test/helpers.js"))
);
const source = fs.readFileSync(root + "/test/spotify-tools.test.js", "utf8");
const { cases, track, album, device } = new Function(
  source.slice(
    source.indexOf("const track ="),
    source.indexOf("for (const mode of"),
  ) + "\nreturn {cases,track,album,device};",
)();
const rows = [];
let catalog;
const playing = {
  is_playing: true,
  item: { ...track, uri: "spotify:track:track1" },
  device,
};
const playRequest = {
  url: "me/player/play?device_id=device1",
  method: "PUT",
  body: {
    context_uri: "spotify:album:album1",
    offset: { uri: "spotify:track:track1" },
  },
};
const trackLookup = {
  url: "tracks/track1",
  response: { album: { uri: "spotify:album:album1" } },
};
const devices = { url: "me/player/devices", response: { devices: [device] } };
const variants = [
  ...[
    ["track-missing-album", {}],
    ["track-invalid-album", { album: { uri: "spotify:playlist:wrong" } }],
  ].map(([variant, response]) => ({
    name: "playMusic",
    variant,
    args: { uri: "spotify:track:track1" },
    http: [{ url: "tracks/track1", response }],
  })),
  {
    name: "playMusic",
    variant: "track-lookup-failure",
    args: { uri: "spotify:track:track1" },
    http: [
      {
        ...trackLookup,
        status: 403,
        response: { error: { message: "Forbidden" } },
      },
    ],
  },
  ...[
    ["playback-not-started", undefined],
    ["playback-paused", { ...playing, is_playing: false }],
    [
      "playback-wrong-track",
      { ...playing, item: { uri: "spotify:track:other" } },
    ],
    [
      "playback-wrong-device",
      { ...playing, device: { ...device, id: "other" } },
    ],
    [
      "playback-muted",
      { ...playing, device: { ...device, volume_percent: 0 } },
    ],
  ].map(([variant, response]) => ({
    name: "playMusic",
    variant,
    args: { uri: "spotify:track:track1" },
    http: [
      trackLookup,
      devices,
      playRequest,
      ...Array.from({ length: variant === "playback-muted" ? 1 : 4 }, () => ({
        url: "me/player",
        response,
      })),
    ],
  })),
  {
    name: "playMusic",
    variant: "play-request-failure",
    args: { uri: "spotify:track:track1" },
    http: [
      trackLookup,
      devices,
      {
        ...playRequest,
        status: 403,
        response: {
          error: { message: "Unexpected token in upstream response" },
        },
      },
    ],
  },
  {
    name: "playMusic",
    variant: "delayed-playback",
    args: { uri: "spotify:track:track1" },
    http: [
      trackLookup,
      devices,
      playRequest,
      { url: "me/player", response: { ...playing, is_playing: false } },
      { url: "me/player", response: playing },
    ],
  },
  {
    name: "playMusic",
    variant: "relinked-track",
    args: { uri: "spotify:track:track1" },
    http: [
      trackLookup,
      devices,
      playRequest,
      {
        url: "me/player",
        response: {
          ...playing,
          item: {
            uri: "spotify:track:replacement",
            linked_from: { uri: "spotify:track:track1" },
          },
        },
      },
    ],
  },
  ...["album", "artist", "playlist"].map((type) => ({
    name: "playMusic",
    variant: type + "-context",
    args: { context_uri: `spotify:${type}:${type}1`, offset: 2 },
    http: [
      devices,
      {
        ...playRequest,
        body: {
          context_uri: `spotify:${type}:${type}1`,
          offset: { position: 2 },
        },
      },
      {
        url: "me/player",
        response: {
          ...playing,
          context: { uri: `spotify:${type}:${type}1` },
        },
      },
    ],
  })),
  {
    name: "playMusic",
    variant: "playback-wrong-context",
    args: { context_uri: "spotify:playlist:playlist1" },
    http: [
      devices,
      { ...playRequest, body: { context_uri: "spotify:playlist:playlist1" } },
      ...Array.from({ length: 4 }, () => ({
        url: "me/player",
        response: {
          ...playing,
          context: { uri: "spotify:playlist:other" },
        },
      })),
    ],
  },
  {
    name: "adjustVolume",
    variant: "no-device",
    args: { adjustment: 10 },
    http: [{ url: "me/player", status: 204 }],
  },
  {
    name: "adjustVolume",
    variant: "unknown-volume",
    args: { adjustment: 10 },
    http: [
      {
        url: "me/player",
        response: { device: { ...device, volume_percent: null } },
      },
    ],
  },
  ...["album", "artist", "playlist", "episode", "show"].map((type) => ({
    name: "searchSpotify",
    variant: type,
    args: { query: "test", type },
    http:
      type === "episode"
        ? [
            {
              url: "search?q=test&type=episode&limit=10&offset=0&market=from_token",
              response: { episodes: { items: [{ id: "episode1" }] } },
            },
            {
              url: "episodes?ids=episode1&market=from_token",
              response: {
                episodes: [
                  {
                    id: "episode1",
                    name: "Test episode",
                    duration_ms: 180000,
                    release_date: "2026-01-01",
                    show: { name: "Test show" },
                  },
                ],
              },
            },
          ]
        : [
            {
              url:
                "search?q=test&type=" +
                type +
                "&limit=10&offset=0" +
                (type === "show" ? "&market=from_token" : ""),
              response: {
                [type + "s"]: {
                  items: [
                    type === "album"
                      ? album
                      : type === "artist"
                        ? { id: "artist1", name: "Test Artist" }
                        : type === "playlist"
                          ? {
                              id: "playlist1",
                              name: "Test Playlist",
                              owner: { display_name: "Test User" },
                              description: "Test description",
                            }
                          : {
                              id: "show1",
                              name: "Test show",
                              publisher: "Test publisher",
                              total_episodes: 1,
                            },
                  ],
                },
              },
            },
          ],
  })),
  ...[true, false].map((is_playing) => ({
    name: "getNowPlaying",
    variant: is_playing ? "playing" : "paused",
    http: [
      {
        url: "me/player",
        response: {
          item: track,
          is_playing,
          device,
          progress_ms: 12000,
          shuffle_state: false,
          repeat_state: "off",
        },
      },
    ],
  })),
  {
    name: "getNowPlaying",
    variant: "podcast",
    http: [{ url: "me/player", response: { item: { type: "episode" } } }],
  },
  {
    name: "getMyPlaylists",
    variant: "empty",
    http: [
      {
        url: "me/playlists?limit=50&offset=0",
        response: { items: [], total: 0 },
      },
    ],
  },
  {
    name: "getMyPlaylists",
    variant: "empty-page",
    args: { offset: 50 },
    http: [
      {
        url: "me/playlists?limit=50&offset=50",
        response: { items: [], total: 3 },
      },
    ],
  },
  {
    name: "getAvailableDevices",
    variant: "empty",
    http: [{ url: "me/player/devices", response: { devices: [] } }],
  },
  {
    name: "searchSpotify",
    variant: "empty",
    args: { query: "test", type: "track" },
    http: [
      {
        url: "search?q=test&type=track&limit=10&offset=0",
        response: { tracks: { items: [] } },
      },
    ],
  },
  { name: "playMusic", variant: "missing-arguments", http: [] },
];
for (const mode of ["legacy", { pin: "2026-07-28" }]) {
  for (const scenario of [...cases, ...variants]) {
    test(`${scenario.name} ${scenario.variant ?? "success"} ${JSON.stringify(mode)}`, async (t) => {
      mockConfig(t);
      mockHttp(t, scenario.http);
      const client = await connect(t, mode);
      if (!catalog) catalog = (await client.listTools()).tools;
      const args = scenario.args ?? {};
      const result = await client.callTool({
        name: scenario.name,
        arguments: args,
      });
      rows.push({
        tool: scenario.name,
        variant: scenario.variant ?? "success",
        protocol: mode,
        args,
        result,
      });
    });
  }
  for (const scenario of cases) {
    test(`${scenario.name} failure ${JSON.stringify(mode)}`, async (t) => {
      mockConfig(t);
      mockHttp(t, [
        {
          ...scenario.http[0],
          status: 403,
          response: { error: { status: 403, message: "Forbidden" } },
        },
      ]);
      const client = await connect(t, mode);
      const args = scenario.args ?? {};
      const result = await client.callTool({
        name: scenario.name,
        arguments: args,
      });
      rows.push({
        tool: scenario.name,
        variant: "failure",
        protocol: mode,
        args,
        result,
      });
    });
  }
}
test.after(() => {
  const paths = [
    "src/read.ts",
    "src/play.ts",
    "src/albums.ts",
    "src/playlist.ts",
    "src/tool.ts",
    "src/utils.ts",
  ];
  fs.writeFileSync(
    new URL("./spotify-results.json", import.meta.url),
    JSON.stringify(
      {
        provenance:
          "Captured from the installed pinned Spotify MCP through its real SDK Client with mocked HTTP and in-memory credentials. No real Spotify requests or writes.",
        source_sha256: Object.fromEntries(
          paths.map((p) => [
            p,
            crypto
              .createHash("sha256")
              .update(fs.readFileSync(root + "/" + p))
              .digest("hex"),
          ]),
        ),
        tools: catalog,
        rows,
      },
      null,
      2,
    ) + "\n",
  );
});
