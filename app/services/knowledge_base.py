"""Knowledge base service: ChromaDB vector store с поддержкой загрузки файлов."""

import io
import uuid
from pathlib import Path
from loguru import logger
from app.core.config import get_settings

try:
    import chromadb
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logger.warning("chromadb not installed — knowledge base disabled")


def _extract_text(file_bytes: bytes, filename: str) -> str:
    """Извлекает текст из файла (txt, pdf, docx)."""
    ext = Path(filename).suffix.lower()

    if ext == ".txt":
        return file_bytes.decode("utf-8", errors="replace")

    if ext == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages)
        except Exception as e:
            raise ValueError(f"Ошибка чтения PDF: {e}")

    if ext == ".docx":
        try:
            import docx
            doc = docx.Document(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            raise ValueError(f"Ошибка чтения DOCX: {e}")

    raise ValueError(f"Неподдерживаемый формат файла: {ext}")


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Разбивает текст на перекрывающиеся куски."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


class KnowledgeBaseService:
    """Векторная БД знаний на базе ChromaDB (embedded, all-MiniLM embeddings)."""

    COLLECTION_NAME = "knowledge"

    def __init__(self):
        self.settings = get_settings()
        self._client = None
        self._collection = None

        if CHROMADB_AVAILABLE:
            try:
                kb_dir = self.settings.knowledge_base_dir
                Path(kb_dir).mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(path=kb_dir)
                self._collection = self._client.get_or_create_collection(
                    name=self.COLLECTION_NAME,
                    metadata={"hnsw:space": "cosine"},
                )
                logger.info(f"Knowledge base initialized at {kb_dir}")
            except Exception as e:
                logger.error(f"Failed to initialize knowledge base: {e}")

    @property
    def available(self) -> bool:
        return self._collection is not None

    async def add_document(self, filename: str, content: str) -> dict:
        """
        Добавляет документ в базу знаний.
        Разбивает на чанки, создаёт эмбеддинги (автоматически ChromaDB).
        """
        if not self.available:
            raise RuntimeError("Knowledge base не инициализирована")

        doc_id = str(uuid.uuid4())
        chunks = _chunk_text(content)

        if not chunks:
            raise ValueError("Файл не содержит текста")

        chunk_ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
        metadatas = [
            {"doc_id": doc_id, "filename": filename, "chunk_index": i}
            for i in range(len(chunks))
        ]

        self._collection.add(
            ids=chunk_ids,
            documents=chunks,
            metadatas=metadatas,
        )

        logger.info(f"Document added: {filename} ({len(chunks)} chunks, id={doc_id})")
        return {"doc_id": doc_id, "filename": filename, "chunks_count": len(chunks)}

    async def search(self, query: str, n_results: int = 3) -> list[str]:
        """Семантический поиск по базе знаний. Возвращает топ-N чанков."""
        if not self.available:
            return []

        try:
            total = self._collection.count()
            if total == 0:
                return []

            n = min(n_results, total)
            results = self._collection.query(
                query_texts=[query],
                n_results=n,
            )
            docs = results.get("documents", [[]])[0]
            return [d for d in docs if d]
        except Exception as e:
            logger.error(f"Knowledge base search error: {e}")
            return []

    def list_documents(self) -> list[dict]:
        """Список загруженных документов (уникальных, без отдельных чанков)."""
        if not self.available:
            return []

        try:
            all_meta = self._collection.get(include=["metadatas"])["metadatas"]
            seen: dict[str, dict] = {}
            for m in (all_meta or []):
                did = m.get("doc_id", "")
                if did and did not in seen:
                    seen[did] = {
                        "doc_id": did,
                        "filename": m.get("filename", "unknown"),
                    }
            return list(seen.values())
        except Exception as e:
            logger.error(f"list_documents error: {e}")
            return []

    def delete_document(self, doc_id: str) -> bool:
        """Удаляет все чанки документа по doc_id."""
        if not self.available:
            return False

        try:
            existing = self._collection.get(
                where={"doc_id": doc_id},
                include=["metadatas"],
            )
            if not existing["ids"]:
                return False
            self._collection.delete(where={"doc_id": doc_id})
            logger.info(f"Document deleted: {doc_id}")
            return True
        except Exception as e:
            logger.error(f"delete_document error: {e}")
            return False


# Хелпер для роутов
extract_text = _extract_text
