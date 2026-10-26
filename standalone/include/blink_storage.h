/* Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE. */
#ifndef BLINK_STANDALONE_INCLUDE_BLINK_STORAGE_H_
#define BLINK_STANDALONE_INCLUDE_BLINK_STORAGE_H_

#include "blink_engine.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum be_storage_mode {
  BE_STORAGE_MEMORY = 0,
  BE_STORAGE_HOST_PERSISTENT = 1,
  BE_STORAGE_DISABLED = 2
} be_storage_mode;

typedef struct be_storage_snapshot {
  uint32_t struct_size;
  uint32_t abi_version;
  const uint8_t* data;
  size_t size;
  void* release_user_data;
  /* Exactly once after a successful load, including malformed snapshots.
   * NULL is allowed only if all returned bytes already have sufficient
   * lifetime. */
  void (*release)(void* release_user_data);
} be_storage_snapshot;

/* Additive ABI 1 extension. Existing engine/page configurations are unchanged.
 * Configure before creating the first page. NULL selects default memory mode.
 * localStorage is isolated by the complete Blink StorageKey, including
 * partition information, and shared by matching documents in this engine.
 * sessionStorage is additionally isolated by page and is always memory-only.
 *
 * load/save/error run on the engine's storage IO thread, never the API thread.
 * Do not reenter the engine or throw exceptions from these callbacks. Keep
 * user_data valid until engine shutdown returns. The host must serialize its
 * own access if this data is also used from other threads.
 *
 * Host persistence is localStorage only. The UTF-8 storage_key is an opaque
 * canonical Blink key, not a filesystem path. Store the versioned snapshot
 * bytes without modification. Snapshots are bounded at 64 MiB and are
 * compatible only with this snapshot format and native byte order. Empty BE_OK
 * load means a new empty area. save must atomically replace that key's complete
 * snapshot before returning BE_OK; crash durability is the host's
 * responsibility.
 *
 * Blink mutations are asynchronous to this backend. Storage failures are sent
 * to error and retained by get_storage_status; they are not synchronous DOM
 * exceptions. A failed load leaves that area unavailable, rather than allowing
 * writes to overwrite unread persistent data. Pump to observe cache rollbacks.
 */
typedef struct be_storage_client {
  uint32_t struct_size;
  uint32_t abi_version;
  uint32_t mode;
  uint32_t reserved; /* Must be zero. */
  void* user_data;
  be_status (*load)(void* user_data,
                    const char* storage_key_utf8,
                    be_storage_snapshot* snapshot);
  be_status (*save)(void* user_data,
                    const char* storage_key_utf8,
                    const uint8_t* snapshot,
                    size_t snapshot_size);
  void (*error)(void* user_data,
                const char* storage_key_utf8,
                be_status status);
} be_storage_client;

BE_EXPORT be_status
be_engine_set_storage_client(be_engine* engine,
                             const be_storage_client* client);
/* Flushes live documents' prior DOM-storage mutations before returning. This
 * can block on host callbacks; it has no timeout. Does not flush IndexedDB,
 * cookies or other browser services. Destruction also flushes live frames. */
BE_EXPORT be_status be_engine_flush_storage(be_engine* engine);
/* The first backend failure is sticky until a pre-page reconfiguration. */
BE_EXPORT be_status be_engine_get_storage_status(be_engine* engine,
                                                 be_status* status_out);

#ifdef __cplusplus
} /* extern "C" */
#endif
#endif
