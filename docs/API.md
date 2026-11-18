# Jdx TTS Studio implementation contract

All API routes are prefixed `/api/v1` and require `Authorization: Bearer <local-session-token>`.
Errors: `{error: {code, message, detail?, request_id?}}`. User messages are simplified Chinese.
Times are integer milliseconds except timeline positions, which use integer samples at 48000 Hz.

## Routes

- `GET /bootstrap` -> `{projects, connections, models, voices, preferences, environment}`
- `GET /health` -> `{status, version}`
- `POST /connections`, `PATCH /connections/{id}` -> connection; accepts ephemeral `api_key` in browser development only; desktop uses credential bridge.
- `DELETE /connections/{id}`; `POST /connections/{id}/test` -> configuration/reachability only, no paid synthesis.
- `GET /projects`, `POST /projects {name}` -> project detail
- `GET /projects/{id}` -> project detail; `PATCH /projects/{id} {revision, name?, lines?, clips?, roles?, video_asset_id?}` -> detail
- `POST /projects/{id}/undo`, `/redo` -> detail
- `POST /projects/{id}/import` multipart `file` -> detail (TXT/SRT/VTT/audio/video)
- `POST /projects/{id}/assets/{asset_id}/relink` multipart `file` -> detail (missing assets only; preserves ID and existing edit ranges)
- `POST /projects/{id}/tasks {line_ids: string[], transport?: "http"|"websocket"}` -> `{jobs: GenerationJob[]}`
- `GET /projects/{id}/tasks` -> jobs array; `POST /projects/{id}/tasks/{job_id}/cancel` -> job
- `POST /projects/{id}/lines/{line_id}/versions/{version_id}/select` -> detail
- `POST /projects/{id}/lines/{line_id}/versions/{version_id}/best` -> detail
- `POST /projects/{id}/export {format:"wav"|"mp3"|"csv"|"sentences"|"project", sample_rate?:48000, bit_depth?:24, normalize?:true, filename?:string}` -> `{job_id}`
- `POST /projects/{id}/render` -> `{job_id}`; render makes a WAV preview consuming the same timeline renderer as export.
- `GET /projects/{id}/exports/{job_id}` -> `{id,status,progress,error?,asset_id?,download_url?}`
- `POST /projects/{id}/backup` -> `{job_id}`; `POST /projects/restore` multipart file -> project
- `POST /projects/{id}/director {text,instruction}` -> `{segments:[{text,instruction,pause_after_ms}],warnings:string[]}`
- `GET /voices`, `POST /voices` -> VoiceProfile; `DELETE /voices/{id}` local removal only
- `PATCH /preferences` merges supplied preference fields; keys such as `first_run_complete` coexist with theme and layout.
- `POST /voices/clone` multipart reference audio + `connection_id,target_model,name,authorized,reference_text?` -> VoiceProfile (poll refresh)
- `POST /voices/design {connection_id,target_model,name,voice_prompt,preview_text}` -> VoiceProfile
- `POST /voices/{id}/refresh` -> VoiceProfile
- `GET /events` authenticated fetch SSE; frontend can poll jobs as fallback
- Media uses signed `media_url` supplied on Asset, with Range support. Download URLs are session scoped and must not be logged.

## Data shapes

CloudConnection: `{id,name,workspace_id,region,base_url,model_id,voice_id,has_credential,max_concurrency}`; default region `cn-beijing`, model `qwen-audio-3.1-tts-plus`, voice `longanhuan_v3.1` (must validate against selected model).
ModelProfile: `{id,name,description,instruction,ssml,streaming,custom_voice,verified}`. SSML further depends on voice capability.
VoiceProfile: `{id,name,kind:"system"|"clone"|"design",connection_id?,target_model,voice_id,status,notes,authorized,ssml?,favorite?}`.
Project: `{id,name,revision,created_at,updated_at,lines:Line[],roles:Role[],clips:TimelineClip[],assets:Asset[],video_asset_id?,can_undo,can_redo}`.
Role: `{id,name,color,voice_id?,connection_id?,model_id?}`.
Line: `{id,text,position,revision,role_id?,settings:LineSettings,selected_version_id?,subtitle_start_ms?,subtitle_end_ms?,versions:AudioVersion[]}`.
LineSettings: `{connection_id?,model_id,voice_id,language,emotion,instruction,rate,pitch,volume,pause_before_ms,pause_after_ms,target_duration_ms?,emphasis_words:string[],ssml:boolean,seed:number}`. Defaults: rate/pitch=1,volume=50,pause_before_ms=0,pause_after_ms=200,language=zh,emotion=自然,seed=0.
AudioVersion: `{id,line_id,asset_id,duration_ms,created_at,settings,model_id,request_id?,usage:object,best,text_revision}`.
Asset: `{id,name,kind:"audio"|"video"|"export",duration_ms,sample_rate,channels,missing,media_url}`.
TimelineClip: `{id,asset_id,line_id?,track_id,label,start_sample,source_start_sample,source_end_sample,gain_db,fade_in_ms,fade_out_ms,muted,stretch}`. `stretch` is playback speed, 1 normal. Source range is in 48000-Hz project samples independent of media source sample rate.
GenerationJob: `{id,line_id,status:"queued"|"running"|"cancel_requested"|"cancelled"|"succeeded"|"failed"|"uncertain"|"interrupted",progress,error?,created_at,request_id?,usage?,version_id?}`.

New line IDs can be client-generated UUIDs. Patch responses return authoritative state. Existing versions/assets are server-owned and cannot be overwritten by project patch. Patches with stale revision return 409.

Connection advanced settings additionally accept `requests_per_minute` (default 120), `price_per_10k_chars`, `input_price_per_million_tokens`, and `output_price_per_million_tokens` (prices optional, user supplied).

LineSettings additionally accepts `pause_after_words: Record<string, integer>` (50–10000 ms) and `pronunciation_overrides: Record<string,string>` (tone-numbered Mandarin pinyin). Both require an SSML-capable voice with `ssml=true`; matching words must occur in text, all occurrences receive the setting. XML is built safely by the backend, not accepted as arbitrary user markup.

Private project state `_auto_clip_positions` records automatically placed clip positions/source lengths for deterministic generation completion ordering; it is never exposed in Project responses. Assets use immutable managed filenames; edits and undo change references only.
