"""In-app visuals: the matte (抠像) model manager (R11) and, later, the
generation pipeline (R9). Heavy ML deps (rembg/onnxruntime) are an OPTIONAL
extra (`uv sync --extra visuals`) and are imported lazily — importing this
package never requires them.
"""
