import unittest

from main import KTVProcessor


class SongNamingTests(unittest.TestCase):
    def test_build_song_filename_prefers_artist_album_title_order(self):
        processor = KTVProcessor(log_cb=lambda msg: None)
        cases = [
            ("Adele - Hello", "", "", "Adele - Hello"),
            ("Hello - Adele", "", "", "Adele - Hello"),
            ("Adele - 25 - Hello", "", "25", "Adele - 25 - Hello"),
            ("Official Music Video: Artist Name - Song Title", "", "", "Artist Name - Song Title"),
            ("Song Title", "The Artist", "The Album", "The Artist - The Album - Song Title"),
        ]
        for title, artist, album, expected in cases:
            with self.subTest(title=title, artist=artist, album=album):
                self.assertEqual(processor.build_song_filename(title, artist, album), expected)

    def test_extract_title_parts_handles_common_patterns(self):
        processor = KTVProcessor(log_cb=lambda msg: None)
        self.assertEqual(processor.extract_title_parts("Adele - Hello"), ("Adele", "", "Hello"))
        self.assertEqual(processor.extract_title_parts("Adele - 25 - Hello"), ("Adele", "25", "Hello"))
        self.assertEqual(processor.extract_title_parts("Official Music Video: Artist Name - Song Title"), ("Artist Name", "", "Song Title"))

    def test_metadata_lyrics_helpers_create_syncable_lrc(self):
        processor = KTVProcessor(log_cb=lambda msg: None)
        terms = processor.extract_lyrics_search_terms("Artist - Album - Song Title")
        self.assertEqual(terms["artist"], "Artist")
        self.assertEqual(terms["track"], "Song Title")

        lrc = processor.generate_lrc_from_lyrics("Line one\nLine two\nLine three")
        self.assertIn("[00:00.000]Line one", lrc)
        self.assertIn("[00:04.000]Line two", lrc)

    def test_rank_best_lyrics_candidate_prefers_best_match(self):
        processor = KTVProcessor(log_cb=lambda msg: None)
        candidates = [
            {"artistName": "Other Artist", "trackName": "Song Title", "plainLyrics": "hello"},
            {"artistName": "Artist", "trackName": "Song Title", "plainLyrics": "hello world"},
            {"artistName": "Artist", "trackName": "Other Song", "plainLyrics": "hello world"},
        ]
        best = processor.rank_lyrics_candidates("Artist", "Song Title", candidates)
        self.assertEqual(best["artistName"], "Artist")
        self.assertEqual(best["trackName"], "Song Title")


if __name__ == "__main__":
    unittest.main()
