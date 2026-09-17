# 🧠 RAG + LLM Project

Simple Retrieval-Augmented Generation (RAG) app built with **Streamlit**, **ChromaDB**, **SentenceTransformers**, and **Ollama** for fully local LLM inference.

Query your own PDF or TXT documents and receive answers generated strictly from your data.

---

## 🚀 Features

- Semantic search with embeddings
- PDF & TXT document ingestion
- Local LLM inference (Ollama)
- Persistent vector database (ChromaDB)
- Clean dark UI with Streamlit
- Context-only answers (no external knowledge)

---

## 🏗 How It Works

1. Documents are split into chunks  
2. Embeddings are generated  
3. Chunks are stored in ChromaDB  
4. User query is embedded  
5. Relevant chunks are retrieved  
6. Ollama generates an answer using only that context  

---

## 🧰 Tech Stack

- Streamlit  
- ChromaDB  
- SentenceTransformers (`all-MiniLM-L6-v2`)  
- PyPDF2  
- Ollama (`llama3.1:8b`)  

---

## ⚙️ Installation

```bash
git clone https://github.com/eduglez03/RAG-LLM-Project.git
cd RAG-LLM-Project
pip install -r requirements.txt
```

### ⚙️ Install and run Ollama

```bash
ollama pull llama3.1:8b
ollama serve
```
---

## 📥 Ingest Documents

Add .pdf or .txt files to the docs/ folder and execute:

```bash
python ingest.py
```
--- 

## ▶️ Run the App

```bash
streamlit run app.py
```

Open in browser:
```bash
http://localhost:8501
```
