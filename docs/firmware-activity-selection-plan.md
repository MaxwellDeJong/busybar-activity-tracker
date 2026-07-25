# Busy Bar Firmware — On-Device Activity Selection: Implementation Plan

## Purpose

Add the ability to define **N activities** on the device and select the active
one **entirely from the hardware** (turning the dial-wheel to scroll, pressing
OK/START to begin), with the host laptop acting as a **purely passive recorder**.
No host process participates in selection. The host only tails the timer state
stream and writes the activity log.

This document is written to be handed to a firmware developer. It references the
actual source in `busybar-firmware` (`dev` branch, `c752cbb2`) as inspected —
every file:line citation below has been verified against that tree.

**Scope assumptions (agreed):**

- **≤ 8 activities.** The front display is a 72×16 LED matrix; scrolling a longer
  list with the wheel gets tedious. `sort_order` therefore matters — put the
  activities you start most often at the top.
- **The currently loaded activity is visually marked** in the picker, so it is
  always clear what is already selected (see §6.4).
- **Selection starts the timer immediately** (no return trip to the Start menu).
- **Activities always write the `Custom` profile slot**, hard-coded. The `BUSY`
  switch position keeps its stock profile untouched.

---

## 1. Background: how selection works today

The dial-to-timer chain in stock firmware:

```
physical switch position
      │  (desktop service routes to the foreground app;
      │   desktop.c:406 maps InputSwitchPositionStatus → "busy" with BUSY_APP_CUSTOM_MODE)
      ▼
main/busy app  ──►  instance->preset_id  ∈ { BusyAppPresetIdBusy, BusyAppPresetIdCustom }
      │              (busy.c:106 sets preset_id from the "custom" launch argument)
      ▼
busy_app_global_presets[preset_id].timer_profile_id  ∈ { BusyTimerProfileIdBusy, BusyTimerProfileIdCustom }
      │
      ▼
busy_timer service holds a BusyTimerProfile per profile id
      │  BusyTimerProfile = { app_config, timer_config, metadata{ title, card_id }, timestamp_ms }
      ▼
timer runs, renders, and STREAMS a snapshot carrying metadata.card_id
      │
      ▼
host reads /api/status/ws → StateUpdate.timer → snapshot.card_id
```

The constraint we must design around: **profiles are a fixed two-entry enum.**

```c
// applications/services/busy_timer/busy_timer_profile.h:14
typedef enum {
    BusyTimerProfileIdBusy,
    BusyTimerProfileIdCustom,
    BusyTimerProfileIdMax,   // used as array size / loop bound throughout
} BusyTimerProfileId;
```

Expanding that enum to N is invasive — `BusyTimerProfileIdMax` sizes arrays and
bounds loops across the timer service, its API, snapshot, and saved-state.

## 2. The chosen approach — model activities as *files*, not as new enum slots

**We do not expand the profile enum.** Instead we keep a single active profile
slot (`Custom`) and *populate it from an unbounded, file-backed list of
activities* chosen on-device. This mirrors a pattern the firmware already
implements for **themes**: `busy_scene_setup_theme.c` walks `BUSY_THEMES_DIR`,
loads an arbitrary number of files into a list, sorts, presents an
encoder-scrolled picker on the device display, and persists the choice.

Benefits:

- **On-device selection** by the encoder — satisfies the hard requirement; the
  host is never in the selection loop.
- **N activities** with no change to `BusyTimerProfileIdMax` or the two-slot API.
- **Change stays inside `applications/main/busy/`** (a scene + a small loader)
  plus a resource folder — the recoverable zone. It does **not** touch
  `targets/`, USB/network bring-up, the bootloader, the protobuf transport, or
  provisioning/OTP.
- **An activity file is just a serialized profile.** The firmware already ships
  `busy_timer_profile_serialize()` / `busy_timer_profile_deserialize()`
  (`busy_timer_profile.h:27`), so loading an activity is deserializing a profile.

### 2.1 Two corrections to the naive "clone the theme picker" reading

The theme scene is the right *shape*, but two of its components are the wrong
things to copy. Both were confirmed by reading the source:

**(a) Do not clone `theme_picker`.** It renders no text at all — it is an
`Image` / `AnimPlayer` showing the theme's background artwork
(`widgets/theme_picker.c:77-99`, `image_set_source()` / `anim_player_set_source()`).
Cloning it for a list of activity *titles* means writing text rendering from
scratch.

The right base is the stock **`Menu` module**
(`applications/services/gui/modules/menu.h`), which `busy_scene_setup.c` already
drives on **both** the 72×16 front display and the back display, with label,
sub-label, icon, and scrollbar. This collapses the "new picker widget + picker
model" work to near zero: the scene builds a `Menu` with one item per activity.
It also removes the need for `DisplayMirror` — follow `busy_scene_setup.c`'s
front-menu/back-menu pair instead of the theme scene's mirror.

**(b) Do not use `busy_timer_set_preset()`.** See §3.

### Selection flow after the change

```
switch → CUSTOM position → main/busy → Setup menu → "Activity"
      → Activity scene (Menu; wheel scrolls the N activities on the display,
        the currently loaded one is marked with a checkmark and pre-focused)
      → OK/START selects
          → deserialize activity file → BusyTimerProfile
          → restamp timestamp_ms  (mandatory — see §3.2)
          → busy_timer_set_profile(Custom, &profile)   // carries metadata.card_id
          → busy_set_app_config(&profile.app_config)   // applies the theme
          → next scene: Overview (INTERVAL) or Timer (SIMPLE/INFINITE),
            which starts the timer itself
      → timer streams snapshot with card_id = activity's UUID
      → host maps card_id → activity key → writes JSONL   (unchanged)
```

## 3. The API path: `set_profile`, not `set_preset`

This section replaces what an earlier draft filed as "the one code path to
confirm". It has been confirmed, and it resolves **negative** — the naive path
does not work.

### 3.1 `set_preset` silently drops `card_id`

`busy_timer_set_preset_api_message_handler` (`busy_timer.c:1024-1027`) copies
only `app_config`, `timer_config` and `timestamp_ms`. It **never touches
`profile->metadata`.** Meanwhile the streamed `card_id` originates from
`instance->card_id`, which is set by `busy_timer_apply_profile_settings()`
(`busy_timer.c:773`) out of `profile->metadata.card_id`, and copied into the
snapshot at `busy_timer.c:428`.

So a `set_preset`-based design would run the right timer with the **wrong
`card_id`** — the host would attribute every session to whatever activity was
loaded previously. Silent, and exactly the kind of bug that corrupts a log
before you notice.

**Use `busy_timer_set_profile()`** (`busy_timer.h:119`), which copies the whole
`BusyTimerProfile` including `metadata`. It is public API; the service still
needs no changes.

### 3.2 `set_profile` has a staleness gate — you must restamp

`busy_timer_set_profile_internal()` (`busy_timer.c:641-650`) rejects any profile
whose `timestamp_ms` is **less than or equal to** the one currently stored,
logging only at `FURI_LOG_D`:

```c
if(profile_timestamp_ms < current_timestamp_ms) { ...RejectedOutdated; break; }
else if(profile_timestamp_ms == current_timestamp_ms) { ...RejectedOwn; break; }
```

Activity files on disk have a fixed timestamp, so the second selection of the
same activity would be a **silent no-op**. The scene must therefore set
`profile.timestamp_ms = furi_hal_rtc_get_timestamp_ms()` immediately before
every `busy_timer_set_profile()` call.

### 3.3 Accepted side effects

`set_profile` also calls `busy_timer_settings_save()` and
`busy_timer_schedule_publish_profile()` (`busy_timer.c:987-992`). Every
selection therefore (a) overwrites the persisted `Custom` profile in flash and
(b) republishes it over MQTT if the device is provisioned. Both are acceptable:
flash wear at human selection rates is negligible, and (a) is in fact desirable —
the chosen activity survives a reboot. Metadata does persist:
`busy_timer_settings_interface_v1.c:230-259` serializes `sort_order`, `title`
and `card_id`.

## 4. Activity JSON schema

An activity file is a serialized `BusyTimerProfile`, parsed by
`busy_timer_profile_deserialize()`. **The parser is strict** — several fields an
earlier draft called optional are in fact required, and a partially-specified
`busy_bar_settings` fails the entire parse.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `title` | string (≤128 bytes UTF-8) | **yes** | Display name in the picker. `BUSY_TIMER_TITLE_LEN` = 32*4. Required at `busy_timer_profile.c:57`. |
| `id` | string (36-char UUID) | **yes** | Becomes `metadata.card_id`; streamed in the snapshot. **The join key the host maps to an activity key.** Validated char-by-char by `busy_timer_common_is_valid_card_id()` (`busy_timer_common.c:199`): hex digits with `-` at positions 8/13/18/23. |
| `sort_order` | integer | **yes** | Required — `busy_timer_profile.c:50` breaks the parse if it is not a number. Orders items in the picker. |
| `profile_timestamp_ms` | integer | **yes** | Required — `busy_timer_profile.c:169`. Put `0` in the file; the scene overwrites it at selection time (§3.2). |
| `timer_settings` | object | **yes** | One of the three modes below. |
| `busy_bar_settings` | object | no | **All-or-nothing**: omit it entirely, or supply all three fields. See §4.1. |

`timer_settings` — one of:

```jsonc
// INTERVAL (pomodoro): the mode you want for deep work
{
  "type": "INTERVAL",
  "interval_work_ms": 1500000,          // 25 min. Range 5..480 min (BUSY_TIMER_WORK_TIME_*).
  "interval_rest_ms": 300000,           // 5 min.  Range 5..480 min (BUSY_TIMER_REST_TIME_*).
  "interval_work_cycles_count": 3,      // Range 2..35 (BUSY_TIMER_CYCLE_COUNT_*).
  "is_autostart_enabled": false
}

// SIMPLE (single countdown)
{ "type": "SIMPLE", "total_time_ms": 1500000 }   // Range 5..1440 min (BUSY_TIMER_TIME_*).

// INFINITE (count-up / open-ended)
{ "type": "INFINITE" }
```

### 4.1 `busy_bar_settings` is all-or-nothing

`busy_timer_common_deserialize_app_config()` (`busy_timer_common.c:59-95`)
requires **all three** of `theme`, `show_work_phase_only`, `trigger_smart_home`,
and breaks on the first one missing or mistyped. The fallback
`busy_timer_profile_handle_missing_app_config()` (`busy_timer_profile.c:108`)
only accepts an **absent or null** block — it does not rescue a partial one. So
a `busy_bar_settings` containing just `{"theme": "coding"}` fails the whole
profile parse and the activity vanishes from the picker with no user-visible
explanation.

```jsonc
{
  "theme": "coding",            // must match an installed theme dir name
  "show_work_phase_only": false,
  "trigger_smart_home": true
}
```

If `theme` names a theme that is not installed, `busy_set_app_config()`
(`busy.c:318-321`) logs a warning and falls back to the default theme — a soft
failure, not a parse failure.

### 4.2 Example activity file — `technical_reading.activity`

```json
{
  "sort_order": 10,
  "title": "Technical Reading",
  "id": "a1b2c3d4-0000-4000-8000-000000000001",
  "profile_timestamp_ms": 0,
  "timer_settings": {
    "type": "INTERVAL",
    "interval_work_ms": 1500000,
    "interval_rest_ms": 300000,
    "interval_work_cycles_count": 3,
    "is_autostart_enabled": false
  },
  "busy_bar_settings": {
    "theme": "coding",
    "show_work_phase_only": false,
    "trigger_smart_home": true
  }
}
```

Assign each activity a **stable, unique `id`**. That UUID is what the host's log
maps to your activity key (`technical_reading`), so it must never change once
sessions have been recorded against it — the same stable-key discipline from the
host schema, enforced here.

## 5. On-device storage layout

Activities are plain files in a dedicated directory, alongside how themes live
today (`BUSY_THEMES_DIR` is defined in `busy_theme.h:7`, not in
`storage_macros.h`). Add the new path macro to
`applications/main/busy/storage_macros.h`:

```c
// existing:
// #define BUSY_ASSETS_PATH(path) EXT_PATH("apps_assets/busy") "/" path
#define BUSY_ACTIVITIES_DIR BUSY_ASSETS_PATH("activities")
```

Seed the `*.activity` files as resources under
`applications/main/busy/resources/apps_assets/busy/activities/`. They are
provisioned onto the device by the normal resource upload (`./fbt flash_usb`
includes resources, or `./fbt resources_upload`). You can add or edit files later
without a reflash.

## 6. Files to change / add

### 6.1 New files

| File | Purpose |
|------|---------|
| `applications/main/busy/helpers/activity_list.{h,c}` | Loads and holds the activity list: walks `BUSY_ACTIVITIES_DIR`, reads each file, `busy_timer_profile_deserialize()` + `busy_timer_profile_is_valid()`, stores an `m-array` of `BusyTimerProfile`, sorts by `sort_order` then `title`. **Replaces the "picker model" of the earlier draft** — the `Menu` widget holds display state itself, so there is no separate model to write. |
| `applications/main/busy/scenes/busy_scene_setup_activity.c` | The new scene: build the `Menu` pair from the list, mark + pre-focus the active activity, handle selection → load + start. |
| `applications/main/busy/resources/apps_assets/busy/activities/*.activity` | Seed activities (e.g. `technical_reading.activity`, `deep_work.activity`, `writing.activity`). |

**No new widget file.** The earlier draft's `widgets/activity_picker.{h,c}` is
dropped — the stock `Menu` module covers it (§2.1a).

### 6.2 Modified files

| File | Change |
|------|--------|
| `applications/main/busy/scenes/busy_scenes.h` | Add `BusyAppSceneIdSetupActivity` to the `BusyAppSceneId` enum (before `BusyAppSceneIdMax`). |
| `applications/main/busy/scenes/busy_scenes.c` | Add `extern const Scene busy_scene_setup_activity;` and the array entry `[BusyAppSceneIdSetupActivity] = &busy_scene_setup_activity,`. |
| `applications/main/busy/scenes/busy_scene_setup.c` | Add an **"Activity"** item to the setup menu (front **and** back); route its custom event to `BusyAppSceneIdSetupActivity`. |
| `applications/main/busy/storage_macros.h` | Add `BUSY_ACTIVITIES_DIR`. |

**`application.fam` needs no edit.** Sources are gathered per app folder by
`GatherSources` (`lib/firmware_applications_si917/SConscript:85-95`), not
enumerated in the manifest. Dropping `.c` files under
`applications/main/busy/` is sufficient.

Note: `busy_timer` service, its public API, `busy_timer_snapshot.c`,
saved-state, and the profile enum are **not modified**. The active profile
remains `Custom`; we only rewrite its contents per selection via the existing
public `set_profile` path.

## 7. Scene wiring — concrete

### 7.1 Register the scene

```c
// busy_scenes.h — add to the enum
typedef enum {
    /* ...existing... */
    BusyAppSceneIdSetupTheme,
    BusyAppSceneIdSetupActivity,   // NEW
    BusyAppSceneIdSetupSmartHome,
    BusyAppSceneIdShowTimer,
    BusyAppSceneIdMax,
} BusyAppSceneId;

// busy_scenes.c — declare + register
extern const Scene busy_scene_setup_activity;                       // NEW
/* ... */
[BusyAppSceneIdSetupActivity] = &busy_scene_setup_activity,         // NEW
```

### 7.2 Route into it from the Setup menu (`busy_scene_setup.c`)

Add a menu index and item, mirroring the existing THEME item. Note the stock
scene adds every item **twice** — once to `front_menu`, once to `back_menu` with
the 11×11 icon variant — and the back copy passes `NULL` for the callback.

```c
#define ITEM_LABEL_ACTIVITY "Activity"

typedef enum {
    BusySceneSetupMenuIndexActivity,   // NEW — first, so it is the top item
    BusySceneSetupMenuIndexTimer,
    BusySceneSetupMenuIndexTheme,
    BusySceneSetupMenuIndexSmartHome,
    BusySceneSetupMenuIndexMax,
} BusySceneSetupMenuIndex;

// in busy_scene_setup_on_enter(), front menu:
menu_add_item(
    data->front_menu, ITEM_LABEL_ACTIVITY, ITEM_SUBLABEL_DUMMY,
    BUSY_IMG_PATH("hourglass_8x8.image"),
    BusySceneSetupMenuIndexActivity,
    busy_scene_setup_menu_callback, instance);
// ...and the matching back-menu item with hourglass_11x11.image, NULL, NULL.

// in busy_scene_setup_on_event():
if(event->event == BusySceneSetupMenuIndexActivity) {
    busy_push_location(instance, "ACTIVITY");
    scene_manager_next_scene(instance->scene_manager, BusyAppSceneIdSetupActivity);
}
```

### 7.3 The loader (`helpers/activity_list.c`)

```c
#define ACTIVITY_FILE_NAME_LEN_MAX (64)
#define ACTIVITY_FILE_SIZE_MAX     (2048)   // an activity JSON is a few hundred bytes

void activity_list_read(ActivityList* list) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* dir = storage_file_alloc(storage);

    do {
        if(!storage_dir_open(dir, BUSY_ACTIVITIES_DIR)) break;

        char file_name[ACTIVITY_FILE_NAME_LEN_MAX];
        while(storage_dir_read(dir, NULL, file_name, sizeof(file_name))) {
            // read the file into a heap buffer (≤ ACTIVITY_FILE_SIZE_MAX), then:
            BusyTimerProfile profile;
            if(busy_timer_profile_deserialize(&profile, json_text, json_len) &&
               busy_timer_profile_is_valid(&profile)) {
                activity_list_add(list, &profile);
            } else {
                FURI_LOG_W(TAG, "Skipping invalid activity: %s", file_name);
            }
        }
        storage_dir_close(dir);
    } while(false);

    storage_file_free(dir);
    furi_record_close(RECORD_STORAGE);

    activity_list_sort(list);   // by sort_order, then title
}
```

The `FURI_LOG_W` on the reject branch is not optional politeness — §4 has three
distinct ways a hand-edited file silently disappears from the picker, and this
log is the only way to tell a typo from a missing file.

Note the app's stack is 4 KiB (`application.fam`), so read file contents into a
`malloc`'d buffer rather than a stack array.

### 7.4 The activity scene (`busy_scene_setup_activity.c`)

Structure follows `busy_scene_setup.c` (Menu pair), not the theme scene (mirror).

```c
typedef struct {
    ActivityList* list;
    Menu* front_menu;
    Menu* back_menu;
} BusySceneSetupActivity;

static void busy_scene_setup_activity_menu_callback(uint32_t index, void* context) {
    busy_send_custom_event((BusyApp*)context, index);
}

static void busy_scene_setup_activity_on_enter(void* context) {
    BusyApp* instance = context;
    BusySceneSetupActivity* data =
        scene_manager_get_scene_data(instance->scene_manager, BusyAppSceneIdSetupActivity);

    data->list = activity_list_alloc();
    activity_list_read(data->list);

    // Which activity is loaded right now? (blocking call, safe here: nothing queued yet)
    BusyTimerProfile current;
    busy_timer_get_profile(instance->busy_timer, BusyTimerProfileIdCustom, &current);

    uint32_t active_idx = ACTIVITY_LIST_INVALID_INDEX;

    with_gui(instance->gui, {
        data->front_menu = menu_alloc(instance->front_window);
        data->back_menu  = menu_alloc(instance->back_window);

        const uint32_t count = activity_list_get_count(data->list);
        for(uint32_t i = 0; i < count; ++i) {
            const BusyTimerProfile* a = activity_list_get_item(data->list, i);
            const bool is_active =
                (strcmp(a->metadata.card_id, current.metadata.card_id) == 0);
            if(is_active) active_idx = i;

            char sub_label[16];
            busy_activity_format_summary(a, sub_label, sizeof(sub_label)); // "25/5 x3", "25 min", "∞"

            // menu_add_item does furi_check(icon_source) — the icon may NOT be NULL.
            menu_add_item(
                data->front_menu, a->metadata.title, sub_label,
                is_active ? SHARED_IMG_PATH("checkmark_front_8x8.image")
                          : BUSY_IMG_PATH("hourglass_8x8.image"),
                i, busy_scene_setup_activity_menu_callback, instance);

            menu_add_item(
                data->back_menu, a->metadata.title, sub_label,
                is_active ? SHARED_IMG_PATH("checkmark_back_11x11.image")
                          : BUSY_IMG_PATH("hourglass_11x11.image"),
                i, NULL, NULL);
        }

        if(active_idx != ACTIVITY_LIST_INVALID_INDEX) {
            menu_set_selected_item_index(data->front_menu, active_idx);
            menu_set_selected_item_index(data->back_menu, active_idx);
        }

        widget_set_scrollbar_enabled(menu_get_base(data->front_menu), true);
        widget_set_scrollbar_enabled(menu_get_base(data->back_menu), true);
    });
}

static void busy_scene_setup_activity_on_exit(void* context) {
    BusyApp* instance = context;
    BusySceneSetupActivity* data =
        scene_manager_get_scene_data(instance->scene_manager, BusyAppSceneIdSetupActivity);

    with_gui(instance->gui, {
        menu_free(data->front_menu);
        menu_free(data->back_menu);
    });
    activity_list_free(data->list);
}

const Scene busy_scene_setup_activity = {
    .enter_callback = busy_scene_setup_activity_on_enter,
    .exit_callback  = busy_scene_setup_activity_on_exit,
    .event_callback = busy_scene_setup_activity_on_event,
    .data_size      = sizeof(BusySceneSetupActivity),
};
```

### 7.5 Accept: load the activity and start immediately

This mirrors `busy_scene_start_handle_start()` (`busy_scene_start.c:32-56`) —
with one deliberate deviation, called out below.

```c
static void busy_scene_setup_activity_accept(BusyApp* instance, uint32_t index) {
    BusySceneSetupActivity* data =
        scene_manager_get_scene_data(instance->scene_manager, BusyAppSceneIdSetupActivity);

    BusyTimerProfile profile = *activity_list_get_item(data->list, index);

    // MANDATORY: defeat the staleness gate in busy_timer_set_profile_internal (§3.2).
    profile.timestamp_ms = furi_hal_rtc_get_timestamp_ms();

    // set_profile (NOT set_preset) — carries metadata.card_id through to the snapshot (§3.1).
    busy_timer_set_profile(instance->busy_timer, BusyTimerProfileIdCustom, &profile);

    // Apply the activity's theme / blanking / smart-home flags.
    busy_set_app_config(instance, &profile.app_config);

    // Same display setup the Start scene does before running.
    with_gui(instance->gui, {
        widget_set_visible(nav_bar_get_base(instance->nav_bar), false);
        widget_set_visible(mirror_card_get_base(instance->timer_card), true);
        mirror_card_set_show_header(instance->timer_card, false);
        mirror_card_set_show_footer(instance->timer_card, false);
    });

    busy_prepare_transition(instance, BusyTransitionTypeSelect);

    // Deviation from busy_scene_start_handle_start(): it calls busy_get_timer_preset()
    // to read the mode. We must NOT — that is a *blocking* API round-trip that would
    // race the set_profile we just queued. We already hold the profile, so read it
    // directly.
    if(profile.timer_config.mode == BusyTimerModeInterval) {
        scene_manager_next_scene(instance->scene_manager, BusyAppSceneIdOverview);
    } else {
        scene_manager_next_scene(instance->scene_manager, BusyAppSceneIdTimer);
    }
}

static bool busy_scene_setup_activity_on_event(const SceneManagerEvent* event, void* context) {
    BusyApp* instance = context;
    bool consumed = false;

    if(event->type == SceneManagerEventTypeCustom) {
        if(event->event < BusyCustomEventIndexMax) {   // menu indices only
            busy_scene_setup_activity_accept(instance, event->event);
        }
        consumed = true;
    } else if(event->type == SceneManagerEventTypeBack) {
        busy_pop_location(instance);
    }

    return consumed;
}
```

Two ordering facts that make this safe:

- The scene does **not** call `busy_timer_start()`. `busy_scene_timer_on_enter()`
  does it (`busy_scene_timer.c:526`), for both the Overview→Timer and direct
  Timer paths.
- `busy_timer_set_profile()` and that later `busy_timer_start()` are both
  asynchronous messages on the **same** `api_queue`
  (`busy_timer_api.c:11-16`), so the profile is guaranteed to be applied before
  the start is processed. This is why step (1) must not be a blocking call.

### 7.6 Scene-stack unwinding needs no special handling

The stack becomes `Start → Setup → SetupActivity → Overview/Timer`. When the
timer ends, `busy_return_to_start_scene()` uses
`scene_manager_search_and_switch_to_previous_scene(Start)` (`busy.c:302-306`),
which unwinds the intermediate scenes without delivering Back events — so the
`nav_bar` location stack is left with stale "SETUP"/"ACTIVITY" entries. That
self-heals: `busy_scene_start_on_enter()` calls `nav_bar_reset_location()`.

## 8. Host side — `activity_recorder.py` (built)

The passive recorder is implemented at `../activity_recorder.py` and validated
offline (8 self-tests) and live on hardware. It:

1. Opens `/api/status/ws` via `busylib` (reusing `busy_probe.py`'s proven
   protobuf-JSON decode), reads `StateUpdate.timer` snapshots.
2. Reads `snapshot.card_id` and maps it via `../activity_card_id_map.json` to the
   stable activity key (falls back to the raw UUID if unmapped).
3. Runs a session state machine and appends one JSONL line per completed session
   to `activity_log.jsonl`.

Selection is fully on-device; the host is passive. The `card_id ⇄ activity key`
map is the only new coordination, and it is the generated static config file.

### 8.1 The stream is event-driven, not periodic (important)

The firmware publishes a timer snapshot only on `SnapshotCreated` events — start,
pause, resume, skip, interval-end, stop — **not on ticks**
(`busy_timer.c:522-530`, debounced). A running timer that is merely counting down
emits nothing; the last-published snapshot goes stale (observed: screen at 12 min
left while the last stream/HTTP snapshot was frozen at work-start). This is fine:
the recorder keys on **transitions**, and each session's end timestamp comes from
the transition snapshot itself, which is emitted in real time. Confirmed live —
pause/resume/pause produced three immediate snapshots and one correctly-bounded
`work` session.

On connect the stream may replay a **stale** last snapshot, so the recorder drops
any snapshot whose `snapshot_timestamp_ms` is not newer than the last it acted on
(monotonic guard). A consequence: a recorder started **mid-session** first sees a
stale state and only begins the session at the next transition (marked
`"partial": true`); run it before starting timers to capture whole sessions. (A
future enhancement could seed the current state from `GET /api/busy/snapshot`.)

### 8.2 Session semantics (agreed)

A **session** is one continuous running span for a given activity + phase. Phase
is `work`/`rest` for INTERVAL (by `current_interval` parity), `focus` for
SIMPLE/INFINITE.

- **Request 1 — pause/abandon ends the session at the stop moment.** A mid-work
  pause closes the session; resuming opens a new one. Two spans that sum toward
  the 25 min. End timestamp = the stopping snapshot's time.
- **Request 2 — flow overtime (`--flow-overtime`).** Because interval autostart is
  off, work expiry advances to the rest phase but **waits, paused**, for a manual
  break. In flow mode the recorder keeps the work session open through that wait
  and closes it when the break actually starts running — so work done past the
  timer is counted, and the session ends when you pick break. A genuine mid-work
  pause still splits (it stays in the work phase; the wait is distinguished by the
  flip to a not-yet-started rest phase). Standard mode ends work at the boundary.

- **Disconnect resilience.** On a stream drop, an in-progress session is force
  closed at the last snapshot actually seen, flagged `"truncated": true`, rather
  than extended across the unobserved gap. Its end is a lower bound; a brief blip
  may split one real session into two adjacent records (the gap is not counted).

Each JSONL record: `activity`, `phase`, `start`, `end` (ISO-8601 UTC),
`duration_s`, `interval_index`, `card_id`, `partial`, `truncated`, and
`overtime_s` (work phase in flow mode).

### 8.4 Audio customization (firmware)

Per the owner's preference, the end-of-phase sound now plays **only at the end of
a break (rest)**, never at the end of a work phase, a SIMPLE activity, or full
session completion. Implemented by gating the cue in
`busy_scene_timer_update_tick` to `timer_state == BusyTimerStateRest` and removing
the `session_completed.snd` call in `busy_scene_finish.c`. Net effect: finishing
an activity is silent; a break ending still chimes to prompt a return to work.

### 8.3 Verification status

- ✅ Offline `--self-test`: 8 cases — pause-splits, standard vs flow boundary,
  mid-work-pause-plus-overtime, abandon-during-overtime, INFINITE, unmapped id.
- ✅ Live: real pause/resume/pause on the device logged one correct `work`
  session (`card_id …003 → work`, duration matched the timestamps).

## 9. Build, flash, and recovery

```bash
cd busybar-firmware
./fbt              # build → dist/f22-D/
./fbt flash_usb    # flash main firmware + resources over USB
```

### 9.1 `--signed` is not what it sounds like, and is already the default

An earlier draft prescribed `./fbt flash_usb --signed` and treated proving that
path as the main risk. Both premises were wrong:

- `--signed` **only concerns the Si917 co-processor images.** It sets
  `BUNDLE_SECURITY_FLAGS` in the update manifest to mark the bundle as suitable
  for a secure-boot device, and *actually signs* RPS images only when the Si917
  firmware is in the bundle (`update.scons:47-86`).
- The `flash_usb` preset is already `signed: True`, with
  `use_sil_m4: False, use_sil_nwp: False` (`update.scons:227`). No Si917
  component is in the bundle, so no signing keys are required and none of the
  `UserError` guards fire. **`--signed` is redundant on this preset.**
- The **U5 main firmware is not signature-checked** by this mechanism. The
  device's `firmware_security` field only reports the Si917 flags
  (`api_status.c:80-92`, `NwpSigned | M4Signed`). A locally built main firmware
  therefore flashes and boots on a `"secure"` device.

Use the `*_unsigned` presets only if you ever deliberately flash unsigned Si917
images — which this project never does.

### 9.2 Recovery artifacts (obtained and verified)

The device shipped with release **1.0.2** (`commit 07e850ec`, 2026-07-13), while
this work is based on the unreleased `dev` branch (`c752cbb2`). Flashing a dev
build is therefore a *branch change*, not a like-for-like reflash — so the
recovery image must be the official 1.0.2 bundle, not a self-built one.

Official builds are published via the updater directory
(`applications/system/updater/settings/interface_v2.h:10`):

```
https://update.busy.app/busybar-firmware/directory.json
```

The following are downloaded, SHA-256-verified against that directory, and kept
in `../recovery/`:

| File | Role |
|------|------|
| `busybar-f22-update_signed-1.0.2.tgz` | **Primary recovery.** Full signed bundle: `firmware.dfu`, `resources.tar`, `sil_firmware_signed.rps`, `sil_nwp_signed.rps`, `updater.bin`. Restores the Si917 images that `flash_usb` never touches. |
| `busybar-f22-recovery-1.0.2.dfu` | DFU-level fallback if the normal updater path is unavailable. |
| `busybar-f22-firmware-1.0.2.dfu` | Bare main-firmware image. |

Its manifest matches the device exactly — `target: 22`, `firmware_commit:
07e850ec`, `firmware_branch: "1.0.2"`, `security_flags: 3`
(`NwpSigned | M4Signed`, i.e. the `"secure"` state the device reports).

To restore, POST the bundle to the device's update endpoint (the device is at
`10.0.4.20` over its USB ethernet interface; cf. `scripts/update_over_http.py`):

```bash
python3 scripts/update_over_http.py ../recovery/busybar-f22-update_signed-1.0.2.tgz
```

### 9.3 The intercom pitfall — flashing a custom build without bricking the screen

This is the single most important operational fact discovered so far, learned by
hitting it: a plain `./fbt flash_usb` of a custom build **puts the device into a
"System error, restart device" screen with input locked.** Understand this
before touching the device again.

**Why it happens.** The device is two chips with separate firmware: the **U5**
main processor and the **Si917** wireless co-processor. On every boot they
handshake over an internal serial link (the *intercom*). The handshake
(`intercom_sync_do_handshake`, `intercom_sync.c:76-91`) is a **character-by-
character echo**: each side sends its control string and must receive the *same*
character back. The control string is the firmware's **git short hash**
(`intercom.c:9-11` → `version_get_githash`). `flash_usb` updates **only the U5**
(`update.scons:227`, `use_sil_m4: False, use_sil_nwp: False`) — by design. So a
dev-U5 (`c752cbb2`) ends up talking to a stock-Si917 (`07e850ec`), the strings
differ, the echo never matches, and the supervisor raises
`SupervisorWarningTypeIntercomError` (`supervisor.c:228-249`). **The U5 is
otherwise fully booted** — the HTTP API keeps responding throughout — so it is
not a brick; only the chip-to-chip link is refused.

**Why the obvious fix does not work.** `INTERCOM_DISABLE_VERSION_CHECK` makes a
chip present the literal string `"intercom"` (`intercom.c:5-6`). But the
handshake needs **both** sides to present the *same* string, and we are not
reflashing the Si917 — it keeps presenting `07e850ec`. Disabling on the U5 alone
just swaps one mismatch (`c752cbb2` vs `07e850ec`) for another (`"intercom"` vs
`07e850ec`). It does not help.

**The safer path — pin the U5's intercom string to the stock Si917's.** Build
the U5 with `INTERCOM_FORCE_VERSION` set to the Si917's git hash. Then the dev U5
presents `07e850ec`, matches the untouched stock Si917, and the handshake
passes — while the Si917 (and thus the device's `"secure"` state) is never
touched. The mechanism: `bsb_common_env_init.scons:18-21` turns the build
variable into the C define `INTERCOM_FORCE_VERSION="…"`, which
`intercom.c:7-8` uses as the control string.

**Step 1 — read the Si917's version (do not hard-code it).** It equals the git
hash the device currently reports while on stock firmware:

```bash
curl -s http://10.0.4.20/api/status/firmware   # → "commit_hash":"07e850ec" on 1.0.2
```

Both chips in a release are built from one commit, so the Si917's intercom string
is that `commit_hash`. For the current 1.0.2 device that is **`07e850ec`**. If
the device is ever restored to a different release, re-read this value.

**Step 2 — build with the pin.**

```bash
./fbt flash_usb INTERCOM_FORCE_VERSION=07e850ec
```

This builds `build/f22-firmware-D/flash_usb_f22.tgz`. Note: the target's
*automatic* flash step also inherits the pin (`update.scons:259`) and runs a
host-side pre-flight that compares the pin against the device's **current**
`intercom_version`. On a stock 1.0.2 device that field is absent, so the
pre-flight **aborts the auto-flash** — harmless, and expected on the first flash;
the bundle is already built. Flash it manually in step 3 instead. (After the
first successful pinned flash the device reports `intercom_version: 07e850ec`, so
the pre-flight then passes and the plain target works.)

**Step 3 — flash manually** (the proven upload path; bypasses the pre-flight):

```bash
PYTHONPATH= ./toolchain/x86_64-linux/bin/python3.11 \
    scripts/update_over_http.py --file build/f22-firmware-D/flash_usb_f22.tgz
```

(System `python3` lacks `colorlog`; the toolchain's Python is the one fbt uses.)

**Step 4 — verify.** After reboot, `curl http://10.0.4.20/api/status/firmware`
should show `branch: "dev"` **and** `intercom_version: "07e850ec"`
(`api_status.c:123` reports `intercom_get_version_string()` directly). Confirm the
screen shows **no** error and input responds.

**Residual risk to watch.** Pinning the version *asserts* the two builds' intercom
protocols are wire-compatible; it does not prove it. The version check exists to
catch exactly the case where they are not. If the dev U5's intercom framing ever
diverges from 1.0.2's, the handshake would pass but runtime traffic could fail
(`IntercomStatusErrorFraming`, `intercom_rx.c:64`; heartbeat drops). Watch the
serial log for framing/heartbeat errors after a pinned flash. If they appear, the
fallback is to move both chips onto dev together (`flash_usb_full`, which pulls in
Si917 images — and on this secure device that road needs signed Si917 firmware, a
larger undertaking we have deliberately deferred).

### 9.4 Safety discipline

- Keep all edits inside `applications/main/busy/` (+ the resource folder). Do
  **not** touch `targets/`, USB/network bring-up, the bootloader, or anything
  writing provisioning/OTP (`otp_*` is one-time-programmable and unrecoverable).
- Any `flash_usb` of a custom build **must** carry the
  `INTERCOM_FORCE_VERSION` pin (§9.3), or the device drops to the intercom-error
  screen every boot until reflashed.
- Keep the verified 1.0.2 recovery bundle (§9.2) on hand. It has already been
  exercised once — it cleared a real intercom fault, not a hypothetical one.
- Have the SWD debug board + an ST-Link/CMSIS-DAP on hand as the last-resort path
  (`./fbt flash`) in case a change ever breaks USB enumeration.

## 10. Milestones

### Milestone 0 — baseline (blocking; nothing else starts until this passes)

1. ✅ **Stock build.** `./fbt` on unmodified `dev` (`c752cbb2`) — succeeded;
   artifacts in `dist/f22-D/`.
2. ✅ **Recovery secured.** Official 1.0.2 bundle downloaded and SHA-256-verified
   into `../recovery/`, manifest confirmed to match the device (§9.2). This
   replaces the earlier draft's plan of treating a self-built image as the
   recovery guarantee, which would not have restored the shipped release.
3. ⚠️ **Baseline flash — attempted, reverted.** `./fbt flash_usb` (unpinned) put
   the dev U5 on a stock 1.0.2 Si917 and triggered the intercom-error screen
   (§9.3). The prepared 1.0.2 recovery bundle restored the device cleanly
   (`branch:1.0.2`, `firmware_security:"secure"`, `intercom_version` gone,
   Busy app streaming) — the recovery drill (§11 test 9) thus passed for real,
   ahead of schedule. **The device is currently back on stock 1.0.2.**
4. ✅ **Baseline flash — pinned.** Reflashed via the
   `INTERCOM_FORCE_VERSION=07e850ec` procedure (§9.3). Device now reports
   `branch:"dev"`, `commit_hash:"c752cbb2"`, `intercom_version:"07e850ec"`,
   `firmware_security:"secure"`, `nwp_version` present (intercom link up), Busy
   app streaming — no error screen. **Milestone 0 complete; the device runs the
   dev U5 on a stock signed Si917.** This is a deliberate, reversible trade —
   reversible via the proven step-2 recovery bundle.

### Milestone 1 — loader + seed files ✅ (code complete, compiles)

`storage_macros.h` macro, `helpers/activity_list.{h,c}`, and the seed
`*.activity` resources — all written and building (`./fbt` exit 0). The loader
walks `BUSY_ACTIVITIES_DIR`, reads each file into a heap buffer, runs
`deserialize` + `is_valid`, skips bad files with `FURI_LOG_W`, sorts by
sort_order→title, and logs each loaded activity + a total count. It also exposes
`activity_list_find_by_card_id()` for the Milestone 2 highlight.

Seed activities (UUIDs `b1a70000-…-0000000000N`), current configs:

| Activity | Mode |
|----------|------|
| Rec Reading | INFINITE |
| Tech Reading | INFINITE |
| Work | INTERVAL 25/5 ×4 |
| Development | INTERVAL 25/5 ×2 |
| Exercise | INFINITE |
| Perfect Form | INTERVAL 25/5 ×2 |

Readings are INFINITE so reading past a target records the true elapsed time
(SIMPLE would hard-stop at the target and cap the log). Durations are editable
per file without code changes.

**Runtime verification is deferred to the Milestone 2 flash** — the loader has no
caller until the scene invokes `activity_list_read()`, so rather than spend a
throwaway pinned flash on a temporary boot hook, we verify the load logs and the
picker together once the scene exists.

### Milestone 2 — scene ✅ (code complete, compiles)

Done and building (`./fbt` exit 0, +1 flash page):
- `busy_scenes.h/.c` — `BusyAppSceneIdSetupActivity` registered.
- `busy_scene_setup.c` — "Activity" item added as the **top** setup entry
  (front + back menus) with its route.
- `busy_scene_setup_activity.c` — builds the front/back `Menu` from the list,
  sub-labels each with a timer summary (`25/5 x4`, `45 min`, `Open`), marks the
  loaded activity with a checkmark icon and pre-focuses it, and on select:
  restamp → `set_profile(Custom)` → force `preset_id=Custom` →
  `busy_set_app_config` → mirror the Start display setup → navigate to
  Overview (INTERVAL) / Timer (else), which starts the timer.

Two ordering guarantees make the accept path safe (both verified against source):
the accept handler reads the mode from the in-hand profile rather than a blocking
query, and `set_profile` + the timer scene's later `busy_timer_start` are FIFO on
the one `api_queue`, so the start always sees the freshly written profile.
Forcing `preset_id=Custom` ensures the timer scene's
`busy_timer_start(busy_get_profile_id(...))` targets the slot we wrote, even if
the physical switch is in the BUSY position.

### Milestone 3 — verification ✅ (host-connected checks passed on device)

Flashed via the pinned procedure (auto-flash this time: the device already
reported `intercom_version:07e850ec`, so the pre-flight passed). Verified on
hardware:

- ✅ **Provisioning** — all 6 `*.activity` files present under
  `/ext/apps_assets/busy/activities/` (`/api/storage/list`), content intact.
- ✅ **Loader** — device log shows all 6 parsed, **zero** failures, `Loaded 6
  activities`.
- ✅ **Picker** — 6 rows in sorted order with timer sub-labels; on-device wheel
  scroll + select works.
- ✅ **Highlight** — after a selection, the active activity shows the checkmark
  and the cursor pre-focuses it.
- ✅ **card_id propagation (linchpin, §3.1)** — after selecting Tech Reading the
  live snapshot streams `card_id: b1a70000-…-000000000002`, the exact file UUID;
  timer config + theme match the file.
- ✅ **Restamp / re-selection (§3.2)** — selecting the same activity twice
  advances the Custom profile `profile_timestamp_ms` (…943835 → …093757) and
  starts cleanly; no silent no-op.

Remaining: the **host-off acceptance test** — select an activity with the laptop
fully closed and confirm the timer runs (proves the host is out of the selection
loop). This is run independently since it removes API visibility.

Per §11 for the full matrix.

## 11. Test plan

1. **Loader**: drop 3 `*.activity` files; confirm all appear, sorted, with
   correct titles and sub-labels.
2. **Strict-parser negatives**: temporarily drop a file missing
   `profile_timestamp_ms`, one missing `sort_order`, and one with a partial
   `busy_bar_settings`. Each must be absent from the picker **and** produce the
   `FURI_LOG_W` from §7.3. This is the guard against silently losing an activity
   to a typo.
3. **Highlight**: the currently loaded activity shows the checkmark icon and the
   cursor starts on it; after selecting a different one and re-entering the
   scene, the checkmark has moved.
4. **Selection is on-device with host off**: close the laptop entirely. Flip to
   CUSTOM, open Activity, scroll with the wheel, press START. The chosen
   activity's timer must run and display with **no host connected** — this is the
   hard requirement's acceptance test.
5. **`card_id` propagation**: reconnect host, run `busy_probe.py`, confirm the
   streamed `snapshot.card_id` matches the selected activity's UUID. Repeat for a
   second, different activity to prove it actually changes.
6. **Re-selection / staleness gate**: select the *same* activity twice in a row.
   The second run must start normally and stream the same `card_id` — this is the
   regression test for the §3.2 restamp.
7. **Interval mechanics**: confirm work/rest/cycles honor the file's values and
   that pause/resume and completion emit the expected snapshot transitions the
   recorder keys on.
8. **Reboot persistence**: after selecting an activity, power-cycle; the `Custom`
   profile should still hold that activity (§3.3).
9. **Recovery drill**: reflash the known-good stock image over USB and confirm a
   clean return to baseline.

## 12. Effort summary

- **New code**: one scene (patterned on `busy_scene_setup.c`), one file loader
  reusing `busy_timer_profile_deserialize()`, and a few seed resource files.
  **No new widget** — the stock `Menu` module replaces the picker widget and
  picker model of the earlier draft.
- **Modified code**: 3 small edits — scene enum + registration, one setup-menu
  item + route, one path macro. No `application.fam` change.
- **Untouched**: timer service internals, profile enum, snapshot, saved-state,
  the two-slot API, transport, platform, bootloader, provisioning.
- **Host**: unchanged recorder + one static `card_id → activity key` map.

The design borrows the firmware's own proven on-device list pattern, so the
risky/novel part (hardware list selection) is adaptation rather than invention,
and the whole change stays within the USB-recoverable application layer.
