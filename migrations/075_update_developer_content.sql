-- =============================================================================
-- Migration 075: Update developer page content branding
-- Fix "Digitel" -> "Digital" and "A SRIBEES LANKA (PVT) LTD COMPANY" -> "SRIBEES LANKA (PVT) LTD COMPANY"
-- =============================================================================

-- Data write REMOVED 2026-11-07 (owner's decision): every deploy replays every
-- migration, so this re-write of the developer page text (074 already holds the corrected text) ran again on each release and could change live
-- data. The file stays so the replay list does not change.
