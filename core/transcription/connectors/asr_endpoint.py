"""
ASR Endpoint connector for custom self-hosted ASR services.

Supports whisper-asr-webservice, WhisperX, and other compatible ASR services
that expose a /asr endpoint.

改造自 speakr：
- 第 22 行原 `from src.config.app_config import ASR_ENABLE_CHUNKING, ASR_MAX_DURATION_SECONDS`
  改为从环境变量读取（ASR_ENABLE_CHUNKING / ASR_MAX_DURATION_SECONDS）。
- `timeout` property 原会 fallback 到 SystemSetting (数据库)，core/ 去掉数据库依赖，
  仅用环境变量 + 初始化配置值。
"""

import logging
import os
import httpx
from typing import Dict, Any, Set

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError, ProviderError

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean from an environment variable."""
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes")


def _env_int(name: str, default: int) -> int:
    """Read an int from an environment variable, falling back to default."""
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


class ASREndpointConnector(BaseTranscriptionConnector):
    """Connector for custom ASR webservice (whisper-asr-webservice, WhisperX, etc.)."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.SPEAKER_COUNT_CONTROL,  # Supports min/max speakers
        TranscriptionCapability.HOTWORDS,
        TranscriptionCapability.INITIAL_PROMPT,
    }
    PROVIDER_NAME = "asr_endpoint"

    # SPECIFICATIONS is set dynamically in __init__ based on ASR_ENABLE_CHUNKING config
    # Default values here for class-level reference (overridden per-instance)
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the ASR Endpoint connector.

        Args:
            config: Configuration dict with keys:
                - base_url: ASR service base URL (required)
                - timeout: Request timeout in seconds (default: 1800)
                - return_speaker_embeddings: Whether to request embeddings (default: False)
                - diarize: Whether to enable diarization by default (default: True)
        """
        super().__init__(config)

        self.base_url = config['base_url'].rstrip('/')
        self._config_timeout = config.get('timeout', 1800)  # 30 minutes default
        self.return_embeddings = config.get('return_speaker_embeddings', False)
        self.default_diarize = config.get('diarize', True)

        # Configure chunking behavior based on environment variables.
        # speakr 原版从 src.config.app_config 读取，core/ 改为环境变量：
        #   ASR_ENABLE_CHUNKING=true 开启 app 级分片（防止自建 ASR 在长文件上因显存溢出崩溃）
        #   ASR_MAX_DURATION_SECONDS 分片上限秒数
        asr_enable_chunking = _env_bool('ASR_ENABLE_CHUNKING', default=False)
        asr_max_duration = _env_int('ASR_MAX_DURATION_SECONDS', default=1800)

        if asr_enable_chunking:
            # Calculate recommended chunk size (80% of max for safety margin)
            recommended_chunk = int(asr_max_duration * 0.8)
            self.SPECIFICATIONS = ConnectorSpecifications(
                max_file_size_bytes=None,  # No file size limit
                max_duration_seconds=asr_max_duration,
                handles_chunking_internally=False,  # App handles chunking
                recommended_chunk_seconds=recommended_chunk,
            )
            logger.info(
                f"ASR chunking enabled: max_duration={asr_max_duration}s, "
                f"recommended_chunk={recommended_chunk}s"
            )
        else:
            # Default behavior: ASR service handles everything internally
            self.SPECIFICATIONS = ConnectorSpecifications(
                max_file_size_bytes=None,
                max_duration_seconds=None,
                handles_chunking_internally=True,
            )

        # Add speaker embeddings capability if enabled
        if self.return_embeddings:
            self.CAPABILITIES = self.CAPABILITIES | {TranscriptionCapability.SPEAKER_EMBEDDINGS}

    @property
    def timeout(self):
        """Get ASR timeout, reading fresh from env each time to respect runtime changes.

        speakr 原版在此处会 fallback 到 SystemSetting (数据库 Admin UI 设置)，
        core/ 去掉数据库依赖，仅用环境变量 + 初始化时的配置值。
        """
        # Environment variables take priority
        env_timeout = os.environ.get('ASR_TIMEOUT') or os.environ.get('asr_timeout_seconds')
        if env_timeout:
            try:
                return int(env_timeout)
            except (ValueError, TypeError):
                pass

        # Fall back to config value from initialization
        return self._config_timeout

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not self.config.get('base_url'):
            raise ConfigurationError("base_url is required for ASR endpoint connector")

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """
        Transcribe audio using ASR webservice.

        Args:
            request: Standardized transcription request

        Returns:
            TranscriptionResponse with segments and speaker information
        """
        try:
            url = f"{self.base_url}/asr"

            params = {
                'encode': True,
                'task': 'transcribe',
                'output': 'json'
            }

            if request.language:
                params['language'] = request.language
                logger.info(f"Using transcription language: {request.language}")

            # Determine if we should diarize
            should_diarize = request.diarize if request.diarize is not None else self.default_diarize

            # Send both parameter names for compatibility:
            # - 'diarize' is used by whisper-asr-webservice
            # - 'enable_diarization' is used by WhisperX
            params['diarize'] = should_diarize
            params['enable_diarization'] = should_diarize

            if should_diarize and self.return_embeddings:
                params['return_speaker_embeddings'] = True

            if request.min_speakers:
                params['min_speakers'] = request.min_speakers
            if request.max_speakers:
                params['max_speakers'] = request.max_speakers

            if request.prompt:
                params['initial_prompt'] = request.prompt
            if request.hotwords:
                params['hotwords'] = request.hotwords

            # Per-request model override (issue #266). The whisperx-asr-service
            # fork accepts a `model` query parameter (e.g. large-v3,
            # distil-medium.en) and switches the loaded Whisper model
            # on demand. The upstream onerahmet/whisper-asr-webservice
            # ignores unknown params, so this is safe in both cases.
            if request.model:
                params['model'] = request.model
                logger.info(f"Using per-request model override: {request.model}")

            content_type = request.mime_type or 'application/octet-stream'
            files = {
                'audio_file': (request.filename, request.audio_file, content_type)
            }

            # Configure timeout: generous values for large file uploads
            # Write timeout needs to be high too - large files take time to upload
            timeout = httpx.Timeout(
                None,
                connect=60.0,
                read=float(self.timeout),
                write=float(self.timeout),
                pool=None
            )

            logger.info(f"Sending ASR request to {url} with params: {params} (timeout: {self.timeout}s)")

            with httpx.Client() as client:
                response = client.post(url, params=params, files=files, timeout=timeout)
                logger.info(f"ASR request completed with status: {response.status_code}")
                response.raise_for_status()

                # Parse the JSON response
                response_text = response.text
                try:
                    data = response.json()
                except Exception as json_err:
                    if response_text.strip().startswith('<'):
                        logger.error(f"ASR returned HTML error page (status {response.status_code})")
                        raise ProviderError(
                            f"ASR service returned HTML error page",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )
                    else:
                        raise ProviderError(
                            f"ASR service returned invalid response: {json_err}",
                            provider=self.PROVIDER_NAME,
                            status_code=response.status_code
                        )

            return self._parse_response(data)

        except httpx.HTTPStatusError as e:
            # Capture the upstream response body so the actual error message
            # (e.g. faster-whisper's "Invalid model size 'whisper-tiny'") is
            # preserved instead of being collapsed to just a status code.
            try:
                body = e.response.text
            except Exception:
                body = ''
            body_excerpt = body.strip()
            if len(body_excerpt) > 800:
                body_excerpt = body_excerpt[:800] + '...'
            logger.error(
                f"ASR request failed with status {e.response.status_code}: {body_excerpt}"
            )
            detail = (
                f"ASR request failed with status {e.response.status_code}: {body_excerpt}"
                if body_excerpt
                else f"ASR request failed with status {e.response.status_code}"
            )
            raise ProviderError(
                detail,
                provider=self.PROVIDER_NAME,
                status_code=e.response.status_code
            ) from e

        except httpx.TimeoutException as e:
            logger.error(f"ASR request timed out after {self.timeout}s")
            raise TranscriptionError(f"ASR request timed out after {self.timeout}s") from e

        except ProviderError:
            # try 块内构造的 ProviderError 必须原样上抛：
            # 落入下方兜底 Exception 会被二次包装成 TranscriptionError，
            # 丢失 provider/status_code 信息并破坏调用方按类型分支的逻辑
            raise

        except Exception as e:
            error_msg = str(e)
            logger.error(f"ASR transcription failed: {error_msg}")
            raise TranscriptionError(f"ASR transcription failed: {error_msg}") from e

    def _parse_response(self, data: Dict[str, Any]) -> TranscriptionResponse:
        """
        Parse ASR webservice response into standardized format.

        The ASR response contains:
        - text: Full transcription text
        - language: Detected language
        - segments: Array of segments with speaker, text, start, end
        - speaker_embeddings: Optional speaker embeddings (WhisperX only)
        """
        segments = []
        speakers = set()
        full_text_parts = []
        last_speaker = None

        logger.info(f"ASR response keys: {list(data.keys())}")

        if 'segments' in data and isinstance(data['segments'], list):
            logger.info(f"Number of segments: {len(data['segments'])}")

            for seg in data['segments']:
                speaker = seg.get('speaker')

                # Handle missing speakers by carrying forward from previous segment
                if speaker is None:
                    if last_speaker is not None:
                        speaker = last_speaker
                    else:
                        speaker = 'UNKNOWN_SPEAKER'
                else:
                    last_speaker = speaker

                text = seg.get('text', '').strip()
                speakers.add(speaker)
                full_text_parts.append(f"[{speaker}]: {text}")

                segments.append(TranscriptionSegment(
                    text=text,
                    speaker=speaker,
                    start_time=seg.get('start'),
                    end_time=seg.get('end')
                ))

        # Get the full text
        if 'text' in data and isinstance(data['text'], str):
            full_text = data['text']
        elif full_text_parts:
            full_text = '\n'.join(full_text_parts)
        else:
            full_text = ''

        # Extract speaker embeddings if present
        speaker_embeddings = data.get('speaker_embeddings')
        if speaker_embeddings:
            logger.info(f"Received speaker embeddings for speakers: {list(speaker_embeddings.keys())}")

        logger.info(f"Parsed {len(segments)} segments with {len(speakers)} unique speakers: {sorted(speakers)}")

        return TranscriptionResponse(
            text=full_text,
            segments=segments,
            speakers=sorted(list(speakers)),
            speaker_embeddings=speaker_embeddings,
            language=data.get('language'),
            provider=self.PROVIDER_NAME,
            model="asr-endpoint",
            raw_response=data
        )

    def list_models(self):
        """Probe the ASR service's /v1/models endpoint and return the list.

        The whisperx-asr-service fork (and any OpenAI-compatible variant)
        exposes /v1/models. The upstream onerahmet/whisper-asr-webservice does
        not, in which case we return an empty list.
        """
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{self.base_url}/v1/models")
                if resp.status_code != 200:
                    return []
                data = resp.json().get('data', [])
                return [
                    {
                        'id': m.get('id'),
                        'label': m.get('id'),
                        'owned_by': m.get('owned_by', 'unknown'),
                    }
                    for m in data if m.get('id')
                ]
        except Exception as e:
            logger.warning(f"asr_endpoint /v1/models probe failed: {e}")
            return []

    def health_check(self) -> bool:
        """Check if ASR endpoint is reachable."""
        try:
            with httpx.Client(timeout=10.0) as client:
                # Try common health check endpoints
                for endpoint in ['/health', '/']:
                    try:
                        response = client.get(f"{self.base_url}{endpoint}")
                        if response.status_code < 500:
                            return True
                    except Exception:
                        continue
                return False
        except Exception:
            return False

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Return JSON schema for configuration."""
        return {
            "type": "object",
            "required": ["base_url"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "ASR service base URL (e.g. http://whisper-asr:9000)"
                },
                "timeout": {
                    "type": "integer",
                    "default": 1800,
                    "description": "Request timeout in seconds"
                },
                "diarize": {
                    "type": "boolean",
                    "default": True,
                    "description": "Enable speaker diarization by default"
                },
                "return_speaker_embeddings": {
                    "type": "boolean",
                    "default": False,
                    "description": "Request speaker embeddings (WhisperX only)"
                }
            }
        }
