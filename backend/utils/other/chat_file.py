import base64
from dataclasses import dataclass
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, cast

import openai
from openai import AsyncOpenAI, AssistantEventHandler
from openai.types.beta.threads import TextContentBlock
from openai.types.chat import (
    ChatCompletionContentPartParam,
    ChatCompletionMessageParam,
)
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

import database.chat as chat_db
from models.chat import ChatSession, FileChat
from utils.executors import db_executor, llm_executor, run_blocking
from utils.llm.capabilities import ModelCapabilityUnavailableError, resolve_model_capability
from utils.llm.gateway_client import should_route_features_through_gateway
from utils.llm.gateway_observability import record_direct_exception_surface
from utils.other.local_file_chat import (
    LocalFileChatError,
    answer_local_file_chat,
    delete_local_attachment_record,
    is_local_attachment_id,
    prepare_local_file_chat,
    prepare_local_file_chat_sync,
    require_local_file_records,
    run_local_file_chat_stream,
    upload_local_attachment,
    validate_local_file_selection,
)
import logging

logger = logging.getLogger(__name__)

_FILE_SEARCH_ASSISTANT_MODEL = "gpt-4.1"


@dataclass(frozen=True)
class FileChatCleanupReceipt:
    """Bounded cleanup result; provider identifiers never leave this process."""

    deleted_records: int
    pending_records: int
    pending_session_objects: int


class FileChatCleanupError(LocalFileChatError):
    """Retryable cleanup failure that preserves every unresolved authority."""

    def __init__(self, reason: str, receipt: FileChatCleanupReceipt) -> None:
        self.receipt = receipt
        super().__init__(reason, retryable=True, status_code=503)

    def as_dict(self) -> dict[str, object]:
        detail = super().as_dict()
        detail['cleanup'] = {
            'deleted_records': self.receipt.deleted_records,
            'pending_records': self.receipt.pending_records,
            'pending_session_objects': self.receipt.pending_session_objects,
        }
        return detail


class UnsupportedChatFileError(Exception):
    """A chat attachment this pipeline cannot process.

    The upload routes own the client contract: a file type we cannot handle is bad request
    input, not a server fault. Without this, PIL (an iPhone .heic photo has no decoder) and
    OpenAI Files (an .ogg voice note is not an accepted extension) escape as 500s.
    """


def _unsupported_chat_file_error(file_path: Union[str, Path]) -> UnsupportedChatFileError:
    suffix = Path(file_path).suffix.lstrip('.').lower()
    label = f"'{suffix}' files are" if suffix else "this file type is"
    return UnsupportedChatFileError(f"Unsupported attachment: {label} not supported in chat.")


def _safe_file_chats(files_data: List[Dict[str, Any]]) -> List[FileChat]:
    """Build FileChat objects from raw file docs, skipping (not raising on) a malformed one.

    A legacy or partial file document (missing openai_file_id, mime_type, created_at, ...) must not
    500 the whole chat-file flow. Skip such a record, logging the file id and offending field names,
    mirroring utils.apps._safe_build_app.
    """
    files: List[FileChat] = []
    for f in files_data:
        try:
            files.append(FileChat(**f))
        except ValidationError as e:
            logger.warning(
                "Skipping malformed chat file %s: %s",
                f.get('id'),
                [err['loc'][0] for err in e.errors()],
            )
    return files


def _file_record_transport(record: Dict[str, Any]) -> str:
    """Resolve cleanup authority from the immutable record id/prefix.

    Newer records may carry an explicit transport, but the server-generated
    ``local_`` prefix remains authoritative for records written before that
    field existed. A claimed local transport with a non-local id is retained
    for operator repair rather than being sent to OpenAI.
    """

    file_id = record.get('openai_file_id')
    declared = record.get('transport')
    if is_local_attachment_id(file_id):
        return 'local_extraction'
    if isinstance(file_id, str) and file_id.startswith('local_'):
        return 'invalid'
    if declared == 'local_extraction':
        return 'invalid'
    if isinstance(file_id, str) and file_id:
        return 'openai_assistants'
    return 'none'


def _managed_openai_cleanup_is_configured() -> bool:
    """Require an explicit platform credential before constructing vendor egress."""

    # An explicit deployment disable is a stronger boundary than merely lacking
    # credentials.  Legacy provider-owned objects remain pending for an
    # operator migration/retry; they must never be deleted through the vendor
    # SDK just because an unrelated OpenAI key leaked into the process.
    if os.getenv('FILE_CHAT_TRANSPORT', '').strip().lower() == 'disabled':
        return False
    if not os.getenv('OPENAI_API_KEY', '').strip():
        return False
    configured_base = os.getenv('OPENAI_BASE_URL', '').strip().rstrip('/')
    return configured_base in {'', 'https://api.openai.com/v1'}


_async_openai: AsyncOpenAI | None = None


def _get_async_openai() -> AsyncOpenAI:
    global _async_openai
    if _async_openai is None:
        _async_openai = AsyncOpenAI(timeout=120.0, max_retries=1)
    return _async_openai


def _selected_file_chat_transport() -> str:
    """Resolve deployment authority before any provider client or object call."""

    capability = resolve_model_capability('file_chat')
    if not capability.selected:
        raise ModelCapabilityUnavailableError(
            'file_chat', capability.reason or 'not_configured', retryable=capability.retryable
        )
    if capability.transport == 'local_extraction':
        return capability.transport
    try:
        routed = should_route_features_through_gateway()
    except RuntimeError:
        routed = True
    if routed:
        record_direct_exception_surface(surface='file_chat.openai_files_assistants_vision')
    return 'openai_files_assistants'


class _StreamingCallbackProtocol:
    """Structural protocol for streaming callbacks (AsyncStreamingCallback in retrieval.agentic)."""

    def put_data_nowait(self, text: str) -> None: ...

    async def put_data(self, text: str) -> None: ...

    def end_nowait(self) -> None: ...

    async def end(self) -> None: ...


class File:
    def __init__(self, file_path: Union[str, Path]) -> None:
        self.file_path = Path(file_path)
        self.file_id: Optional[str] = None
        self.thumbnail_path = ""
        self.thumbnail_name = ""
        self.mime_type = ""
        self.file_name = ""
        self.purpose = "assistants"

    def generate_thumbnail(self, size: Tuple[int, int] = (128, 128)) -> None:
        with Image.open(self.file_path) as img:
            file_name = Path(self.file_path).stem  # File name without extension
            assert img.format is not None  # PIL.Image opened from a path always has a format
            file_format = img.format.lower()

            img.thumbnail(size)
            self.thumbnail_name = self._to_snake_case(f"{file_name}_thumbnail.{file_format}")

            thumb_path = self.file_path.parent / self.thumbnail_name

            img.save(thumb_path, format=img.format)
            self.thumbnail_path = str(thumb_path)

    def get_mime_type(self) -> None:
        mime_type, _ = mimetypes.guess_type(self.file_path)
        self.mime_type = str(mime_type)

    def is_image(self) -> bool:
        return self.mime_type.startswith("image")

    @staticmethod
    def _to_snake_case(string: str) -> str:
        string = re.sub(r"[\s\-]+", "_", string)
        # Add an underscore before any capital letter that is preceded by a lowercase or digit
        string = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", string)
        return string.lower()


class FileChatTool:
    def __init__(self, uid: str, chat_session_id: str) -> None:
        self.uid = uid
        self.chat_session_id = chat_session_id

        # Load chat session from database
        session_data = chat_db.get_chat_session_by_id(uid, chat_session_id)
        if not session_data:
            raise ValueError(f"Chat session {chat_session_id} not found for user {uid}")

        self.chat_session = ChatSession(**session_data)

        # Get thread and assistant IDs from session (may be None)
        self.thread_id = self.chat_session.openai_thread_id
        self.assistant_id = self.chat_session.openai_assistant_id

    @staticmethod
    def upload(
        file_path: Union[str, Path],
        *,
        uid: str | None = None,
        file_name: str | None = None,
    ) -> Dict[str, Any]:
        transport = _selected_file_chat_transport()
        if transport == 'local_extraction':
            if not uid:
                raise LocalFileChatError('authenticated_uid_required', retryable=False, status_code=400)
            return upload_local_attachment(file_path, uid=uid, file_name=file_name or Path(file_path).name)
        result: Dict[str, Any] = {}
        file = File(file_path)
        file.get_mime_type()

        if file.is_image():
            try:
                file.generate_thumbnail()
            except UnidentifiedImageError as error:
                # An image mime type Pillow has no decoder for (.heic from an iPhone camera roll).
                raise _unsupported_chat_file_error(file_path) from error
            file.purpose = "vision"

        with open(file_path, 'rb') as f:
            # upload file to OpenAI
            try:
                response = openai.files.create(file=f, purpose=cast(Any, file.purpose))
            except openai.BadRequestError as error:
                # The provider rejects the extension (audio/video, archives it does not index).
                raise _unsupported_chat_file_error(file_path) from error
            if response:
                file.file_id = response.id
                file.file_name = response.filename

                result["file_name"] = response.filename
                result["file_id"] = response.id
                result["mime_type"] = file.mime_type
                if file.is_image():
                    result["thumbnail"] = file.thumbnail_path
                    result["thumbnail_name"] = file.thumbnail_name
        return result

    @staticmethod
    def cleanup_uploaded_local_files(uid: str, files: Sequence[FileChat]) -> None:
        """Reconcile originals uploaded before the file records were committed."""

        for file in files:
            delete_local_attachment_record(uid, {'openai_file_id': file.openai_file_id, 'name': file.name})

    def process_chat_with_file(self, question: str, file_ids: List[str]) -> str:
        """Process chat with file attachments"""
        transport = _selected_file_chat_transport()
        if transport == 'local_extraction':
            validate_local_file_selection(file_ids)
            files_data = chat_db.get_chat_files_desc(self.uid, files_id=file_ids, limit=9)
            files = require_local_file_records(files_data, file_ids)
            prepared = prepare_local_file_chat_sync(self.uid, question, files)
            return answer_local_file_chat(prepared)
        self._ensure_thread_and_assistant()
        answer = self.ask(self.uid, question, file_ids, self.thread_id, self.assistant_id)
        return answer

    async def process_chat_with_file_stream(
        self,
        question: str,
        file_ids: List[str],
        callback: Optional[_StreamingCallbackProtocol] = None,
    ) -> str:
        """Process chat with file attachments (streaming)"""
        transport = _selected_file_chat_transport()
        # Offloaded: the Firestore read is sync and blocks the event loop in this async path.
        # If this pre-stream setup fails, signal the streaming callback's end before propagating
        # (mirrors the _ensure_thread_and_assistant failure path below) so it is not left dangling.
        assert callback is not None  # streaming path always supplies a callback
        prepared = None
        try:
            if transport == 'local_extraction':
                validate_local_file_selection(file_ids)
            files_data = await run_blocking(
                db_executor, chat_db.get_chat_files_desc, self.uid, files_id=file_ids, limit=9
            )
            if transport == 'local_extraction':
                files = require_local_file_records(files_data, file_ids)
                prepared = await prepare_local_file_chat(self.uid, question, files)
            else:
                files = _safe_file_chats(files_data)
        except Exception:
            callback.end_nowait()
            raise

        if transport == 'local_extraction':
            assert prepared is not None
            return await run_local_file_chat_stream(prepared, callback)

        all_images = all(f.is_image() for f in files) if files else False

        if all_images and files:
            logger.info(f"[FileChat] All {len(files)} files are images, using Chat Completions vision API")
            answer = await self._ask_vision_stream(question, files, callback)
            return answer

        # The Assistants setup and stream iterator both use the synchronous
        # OpenAI client. Keep the complete non-vision sequence off the event
        # loop so graph.py can enforce its first-event and total-stream bounds.
        return await run_blocking(llm_executor, self._ensure_and_ask_stream, question, file_ids, callback)

    def _ensure_and_ask_stream(self, question: str, file_ids: List[str], callback: _StreamingCallbackProtocol) -> str:
        """Run the synchronous Assistants setup and stream in the LLM executor."""
        try:
            self._ensure_thread_and_assistant()
        except Exception:
            # ask_stream owns its callback finalizer; setup fails before that
            # function is entered, so terminate the callback here instead.
            callback.end_nowait()
            raise
        return self.ask_stream(self.uid, question, file_ids, self.thread_id, self.assistant_id, callback)

    async def _ask_vision_stream(
        self,
        question: str,
        files: List[FileChat],
        callback: Optional[_StreamingCallbackProtocol] = None,
    ) -> str:
        """Use Chat Completions API with vision for image-only chats (streaming)"""
        assert callback is not None
        output_list: List[str] = []
        try:
            contents: List[ChatCompletionContentPartParam] = [{"type": "text", "text": question}]
            openai_client = _get_async_openai()
            for file in files:
                file_content = await openai_client.files.content(file.openai_file_id)
                b64 = base64.b64encode(file_content.read()).decode('utf-8')
                mime = file.mime_type or 'image/png'
                contents.append(
                    cast(
                        ChatCompletionContentPartParam,
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "auto"},
                        },
                    )
                )

            messages: List[ChatCompletionMessageParam] = [
                cast(ChatCompletionMessageParam, {"role": "user", "content": contents})
            ]
            stream = await openai_client.chat.completions.create(
                model="gpt-5.6-luna",
                messages=messages,
                stream=True,
                # Luna uses the current Chat Completions output-budget field.
                # `max_tokens` is rejected by the provider with HTTP 400.
                max_completion_tokens=2048,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    await callback.put_data(delta.content)
                    output_list.append(delta.content)
        finally:
            await callback.end()
        return ''.join(output_list)

    def _ensure_thread_and_assistant(self) -> None:
        """Ensure thread and assistant exist, create if needed, and save to database"""
        created_new = False
        timeout = 30.0  # 30 seconds timeout

        # Handle thread
        if self.thread_id:
            # Try to retrieve existing thread
            try:
                thread = openai.beta.threads.retrieve(self.thread_id, timeout=timeout)  # type: ignore[reportDeprecated]  # Assistants API still in use
                logger.info(f"Retrieved existing thread: {thread.id}")
            except Exception as error:
                logger.error('file chat thread retrieval failed error_type=%s', type(error).__name__)
                self.thread_id = None

        if not self.thread_id:
            try:
                thread = openai.beta.threads.create(timeout=timeout)  # type: ignore[reportDeprecated]  # Assistants API still in use
                self.thread_id = thread.id
                created_new = True
                logger.info(f"Created new thread: {self.thread_id}")
            except Exception as error:
                raise RuntimeError('failed to create OpenAI thread') from error

        # Handle assistant
        if self.assistant_id:
            # Try to retrieve existing assistant
            try:
                assistant = openai.beta.assistants.retrieve(self.assistant_id, timeout=timeout)  # type: ignore[reportDeprecated]  # Assistants API still in use
                logger.info(f"Retrieved existing assistant: {assistant.id}")
            except Exception as error:
                logger.error('file chat assistant retrieval failed error_type=%s', type(error).__name__)
                self.assistant_id = None

        if not self.assistant_id:
            try:
                assistant = openai.beta.assistants.create(  # type: ignore[reportDeprecated]  # Assistants API still in use
                    name="File Reader",
                    instructions="You are a helpful assistant that answers questions about the provided file. Use the file_search tool to search the file contents when needed.",
                    # Luna supports vision Chat Completions but not the
                    # Assistants API. Keep file search on an Assistants model.
                    model=_FILE_SEARCH_ASSISTANT_MODEL,
                    tools=[{"type": "file_search"}],
                    timeout=timeout,
                )
                self.assistant_id = assistant.id
                created_new = True
                logger.info(f"Created new assistant: {self.assistant_id}")
            except Exception as error:
                raise RuntimeError('failed to create OpenAI assistant') from error

        # Save to database if we created new ones
        if created_new:
            try:
                chat_db.update_chat_session_openai_ids(
                    self.uid, self.chat_session_id, self.thread_id, self.assistant_id
                )
            except Exception as error:
                logger.error('file chat identifier save failed error_type=%s', type(error).__name__)
                # Continue anyway - IDs will be recreated next time

    def _fill_question(self, uid: str, question: str, file_ids: List[str], thread_id: str) -> None:
        # OpenAI has a limit of 10 items in content array (1 text + max 9 images)
        files = chat_db.get_chat_files_desc(uid, files_id=file_ids, limit=9)

        files_typed = _safe_file_chats(files)

        contents: List[Dict[str, Any]] = []
        attachments: List[Dict[str, Any]] = []

        contents.append({"type": "text", "text": question})

        for file in files_typed:
            if file.is_image():
                contents.append(
                    {"type": "image_file", "image_file": {"file_id": file.openai_file_id, "detail": "auto"}}
                )
            else:
                attachments.append({"file_id": file.openai_file_id, "tools": [{"type": "file_search"}]})

        # ask question
        openai.beta.threads.messages.create(  # type: ignore[reportDeprecated]  # Assistants API still in use
            thread_id=thread_id,
            role="user",
            content=contents,  # type: ignore[arg-type]  # openai accepts a permissive dict shape here
            attachments=attachments,  # type: ignore[arg-type]  # openai accepts a permissive dict shape here
            timeout=30.0,
        )

    def ask(
        self,
        uid: str,
        question: str,
        file_ids: List[str],
        thread_id: Optional[str],
        assistant_id: Optional[str],
    ) -> str:
        assert thread_id is not None and assistant_id is not None  # caller ensures IDs are set
        self._fill_question(uid, question, file_ids, thread_id)

        # Create run and poll for completion (with 2 minute timeout)
        run = openai.beta.threads.runs.create_and_poll(  # type: ignore[reportDeprecated]  # Assistants API still in use
            thread_id=thread_id,
            assistant_id=assistant_id,
            timeout=120.0,  # 2 minutes total timeout
        )

        # Check terminal status
        if run.status == 'completed':
            # Get the messages
            messages = openai.beta.threads.messages.list(thread_id=thread_id, timeout=30.0)  # type: ignore[reportDeprecated]  # Assistants API still in use

            # Return the latest assistant response
            if messages.data and len(messages.data) > 0:
                first_block = messages.data[0].content[0]
                if isinstance(first_block, TextContentBlock):
                    return first_block.text.value
                # Fall back to the original attribute access for any non-text block,
                # which raises AttributeError — matching the prior behavior.
                return first_block.text.value  # type: ignore[union-attr]  # preserve prior crash semantics for non-text blocks

            raise Exception("No response received from assistant")
        else:
            # Handle failed states
            error_msg = f"Run {run.status}"
            if hasattr(run, 'last_error') and run.last_error:
                error_msg += f": {run.last_error.message}"
            raise Exception(error_msg)

    def ask_stream(
        self,
        uid: str,
        question: str,
        file_ids: List[str],
        thread_id: Optional[str],
        assistant_id: Optional[str],
        callback: Optional[_StreamingCallbackProtocol] = None,
    ) -> str:
        assert thread_id is not None and assistant_id is not None and callback is not None

        output_list: List[str] = []

        try:
            self._fill_question(uid, question, file_ids, thread_id)

            with openai.beta.threads.runs.stream(  # type: ignore[reportDeprecated]  # Assistants API still in use
                thread_id=thread_id,
                assistant_id=assistant_id,
                event_handler=AssistantEventHandler(),
                timeout=30.0,
            ) as stream:
                for text in stream.text_deltas:
                    callback.put_data_nowait(text)
                    output_list.append(text)
                stream.until_done()
        finally:
            callback.end_nowait()

        return ''.join(output_list)

    def cleanup(self) -> FileChatCleanupReceipt:
        """Cleanup per-record provider objects without dropping retry authority."""
        logger.info("start cleanup thread chat with file")
        files = chat_db.get_chat_files(self.uid)
        deleted_records: List[Dict[str, Any]] = []
        pending_records: List[Dict[str, Any]] = []
        if files:
            for file_record in files:
                transport = _file_record_transport(file_record)
                if transport == 'local_extraction':
                    try:
                        delete_local_attachment_record(self.uid, file_record)
                    except Exception as error:
                        logger.error('local file chat deletion pending error_type=%s', type(error).__name__)
                        pending_records.append(file_record)
                    else:
                        deleted_records.append(file_record)
                elif transport == 'openai_assistants':
                    openai_file_id = file_record.get('openai_file_id')
                    assert isinstance(openai_file_id, str)
                    if not _managed_openai_cleanup_is_configured():
                        pending_records.append(file_record)
                        continue
                    try:
                        response = openai.files.delete(openai_file_id, timeout=30.0)
                        if getattr(response, 'deleted', True) is False:
                            pending_records.append(file_record)
                        else:
                            deleted_records.append(file_record)
                    except openai.NotFoundError:
                        deleted_records.append(file_record)
                    except Exception as error:
                        logger.error('file chat file deletion pending error_type=%s', type(error).__name__)
                        pending_records.append(file_record)
                elif transport == 'none':
                    # No provider/object authority exists on this malformed
                    # legacy record, so only its database row needs deletion.
                    deleted_records.append(file_record)
                else:
                    pending_records.append(file_record)

            if deleted_records:
                try:
                    chat_db.delete_multi_files(self.uid, deleted_records)
                except Exception as error:
                    logger.error('file chat cleanup receipt persist failed error_type=%s', type(error).__name__)
                    pending_records.extend(deleted_records)
                    deleted_records = []

        pending_session_objects = 0
        for object_kind, object_id in (
            ('thread', self.thread_id),
            ('assistant', self.assistant_id),
        ):
            if not object_id:
                continue
            if not _managed_openai_cleanup_is_configured():
                pending_session_objects += 1
                continue
            try:
                if object_kind == 'thread':
                    openai.beta.threads.delete(object_id, timeout=30.0)  # type: ignore[reportDeprecated]  # Assistants API still in use
                else:
                    openai.beta.assistants.delete(object_id, timeout=30.0)  # type: ignore[reportDeprecated]  # Assistants API still in use
            except openai.NotFoundError:
                pass
            except Exception as error:
                pending_session_objects += 1
                logger.error('file chat session object deletion pending error_type=%s', type(error).__name__)

        receipt = FileChatCleanupReceipt(
            deleted_records=len(deleted_records),
            pending_records=len(pending_records),
            pending_session_objects=pending_session_objects,
        )
        if pending_records or pending_session_objects:
            reason = (
                'managed_cleanup_credential_required'
                if not _managed_openai_cleanup_is_configured()
                else 'attachment_cleanup_pending'
            )
            raise FileChatCleanupError(reason, receipt)
        return receipt
