use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct Song {
    pub id: i64,
    pub title: String,
    pub path: String,
    /// legacy = old OpenKTV left/right; dual = 2 stereo tracks; single = ordinary video
    pub format: String,
}

#[derive(Clone)]
pub struct Library {
    db: PathBuf,
}
impl Library {
    pub fn open(db: impl AsRef<Path>) -> Result<Self> {
        let this = Self { db: db.as_ref().to_path_buf() };
        let conn = this.connection()?;
        conn.execute_batch("PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                format TEXT NOT NULL CHECK(format IN ('legacy','dual','single')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );")?;
        Ok(this)
    }
    fn connection(&self) -> Result<Connection> {
        Connection::open(&self.db).with_context(|| format!("資料庫開啟失敗: {}", self.db.display()))
    }
    pub fn all(&self) -> Result<Vec<Song>> {
        let conn = self.connection()?;
        let mut query = conn.prepare("SELECT id,title,path,format FROM songs ORDER BY id DESC")?;
        let rows = query.query_map([], |r| Ok(Song {
            id:r.get(0)?, title:r.get(1)?, path:r.get(2)?, format:r.get(3)?,
        }))?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }
    /// Read-only, bounded query for the phone controller. The local file paths are
    /// used only by the desktop and are not serialized into HTTP responses.
    pub fn search(&self, keyword: &str, max_results: i64) -> Result<Vec<Song>> {
        let conn = self.connection()?;
        let mut query = conn.prepare("SELECT id,title,path,format FROM songs
            WHERE (?1 = '' OR instr(lower(title), lower(?1)) > 0)
            ORDER BY id DESC LIMIT ?2")?;
        let rows = query.query_map(params![keyword.trim(), max_results.clamp(1, 100)], |r| Ok(Song {
            id: r.get(0)?, title: r.get(1)?, path: r.get(2)?, format: r.get(3)?,
        }))?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn insert(&self, title: &str, path: &str, format: &str) -> Result<Song> {
        if !matches!(format, "legacy" | "dual" | "single") { anyhow::bail!("不支援的歌曲格式"); }
        let conn = self.connection()?;
        conn.execute("INSERT INTO songs(title,path,format) VALUES (?1,?2,?3)", params![title,path,format])?;
        Ok(Song{id:conn.last_insert_rowid(),title:title.into(),path:path.into(),format:format.into()})
    }
    pub fn remove(&self, id: i64) -> Result<()> {
        let conn = self.connection()?;
        conn.execute("DELETE FROM songs WHERE id=?1", [id])?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn library_round_trip() {
        let path=std::env::temp_dir().join(format!("openktv-test-{}.db",uuid::Uuid::new_v4()));
        let db=Library::open(&path).unwrap();
        let song=db.insert("測試歌", "C:/song.mp4", "legacy").unwrap();
        assert_eq!(db.all().unwrap()[0].title, "測試歌");
        assert_eq!(db.search("測試", 80).unwrap().len(), 1);
        assert_eq!(db.search("TEST", 80).unwrap().len(), 0);
        assert!(db.search("找不到", 80).unwrap().is_empty());
        db.remove(song.id).unwrap();
        assert!(db.all().unwrap().is_empty());
        let _=std::fs::remove_file(path);
    }
}
