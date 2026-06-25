
import asyncio
import json
import os
import websockets
from google import genai
from google.genai import types
import base64
from fastapi import WebSocket
from rag_service import build_rag_prompt
from constants import SYSTEM_PROMPT
from dotenv import load_dotenv
load_dotenv()
import os
MODEL = "gemini-3.1-flash-live-preview"
apikey_gem = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=apikey_gem)

SHOPMATE_SYSTEM = SYSTEM_PROMPT
product_detected_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="product_detected",
            description="""
            Call this immediately after identifying a visible product.
            Pass a concise product description.
            """,
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "description": types.Schema(
                        type="STRING"
                    )
                },
                required=["description"]
            )
        )
    ]
)
image_request_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="request_product_images",
            description="""
            Call this ONLY when the user explicitly wants to
            see images, photos, pictures, examples or visual
            representations of products during a normal text
            conversation.

            NEVER call this during:

            - camera activation
            - camera streaming
            - visible product detection
            - product identification
            - similar product search from camera
            - after activate_camera has been called
            """,
            parameters=types.Schema(
                type="OBJECT",
                properties={}
            )
        )
    ]
)
camera_toggle_tool = types.Tool(function_declarations=[types.FunctionDeclaration(name="activate_camera", description="Call this immediately when the user says they want to show a product, hold something up, or share their video feed.", parameters=types.Schema(type="OBJECT", properties={}))])
async def gemini_session_handler(client_websocket):

    try:
        # Receive setup message from browser
        config_message = await client_websocket.receive_text()

        config_data = json.loads(config_message)

        config = config_data.get("setup", {})
        website_id = config_data.get("website_id", config.get("website_id"))
        last_voice_transcript = ""
        last_detected_product = ""
        product_processing = False
        camera_active = False
        camera_session = False
        last_user_text = ""
        gemini_alive = True

        config["tools"] = [camera_toggle_tool, product_detected_tool, image_request_tool]
        config["system_instruction"] = types.Content(parts=[types.Part.from_text(text= SHOPMATE_SYSTEM)])

        print("Received setup:", config)

        async with client.aio.live.connect(
            model=MODEL,
            config=config
        ) as session:

            print("Connected to Gemini API")


            async def handle_tool_call(call_obj):
                nonlocal camera_active
                nonlocal camera_session
                try:
                    await client_websocket.send_json(
                                            json.dumps({
                                            "type": "camera_status",
                                            "status": "opening",
                                            "message": "Opening camera..."
                                        })
                                    )
                    print("--> GEMINI REQUESTED: Activate Camera")
                    camera_active = True
                    camera_session = True
                    await client_websocket.send_text(json.dumps({"action": "CAMERA_ON"}))
                
                    await asyncio.sleep(1.5)

                    await session.send_client_content(turns = types.Content(role="user", parts=[types.Part.from_text(text="Describe the visible product")]), turn_complete=True)

                    function_responses = [
                        types.FunctionResponse(
                            name=call_obj.name,
                            id=call_obj.id,
                            response={"result": "Camera has been opened successfully. Video frames are streaming now."}
                        )
                    ]
                    await session.send_tool_response(function_responses=function_responses)
                    print("--> Tool response sent. Gemini turn unblocked.")
                except Exception as ex:
                    print(f"Error resolving tool call loop: {ex}")

            # =========================
            # Browser -> Gemini
            # =========================

            async def send_to_gemini():
                    nonlocal camera_active
                    nonlocal last_user_text
                    nonlocal gemini_alive

                    print("send_to_gemini started")

                    try:
                        while True:
                            message = await client_websocket.receive_text()

                            if not gemini_alive:
                                print("Gemini session dead. Exiting sender.")
                                break

                            try:
                                data = json.loads(message)

                                # AUDIO
                                if "audio" in data and data["audio"].get("data"):

                                    if not gemini_alive:
                                        break

                                    audio_bytes = base64.b64decode(
                                        data["audio"]["data"]
                                    )

                                    await session.send_realtime_input(
                                        audio=types.Blob(
                                            mime_type="audio/pcm;rate=16000",
                                            data=audio_bytes,
                                        )
                                    )

                                # VIDEO
                                if "video" in data:

                                    if not gemini_alive:
                                        break

                                    if not camera_active:
                                        continue

                                    video_payload = data["video"]["data"]

                                    if "," in video_payload:
                                        video_payload = video_payload.split(",")[-1]

                                    image_bytes = base64.b64decode(
                                        video_payload
                                    )

                                    await session.send_realtime_input(
                                        video=types.Blob(
                                            mime_type="image/jpeg",
                                            data=image_bytes,
                                        )
                                    )

                                # TEXT
                                if "text" in data and data.get("text"):

                                    if not gemini_alive:
                                        break

                                    user_text = data["text"].strip()
                                    last_user_text = user_text

                                    rag_prompt, image_urls = build_rag_prompt(
                                        user_text,
                                        website_id=website_id,
                                    )

                                    await session.send_client_content(
                                        turns=types.Content(
                                            role="user",
                                            parts=[
                                                types.Part.from_text(
                                                    text=rag_prompt
                                                )
                                            ],
                                        ),
                                        turn_complete=True,
                                    )

                                    print(
                                        "Sent text to Gemini:",
                                        user_text
                                    )

                            except Exception as e:
                                print(
                                    f"Error sending to Gemini: {e}"
                                )
                                break

                    except websockets.exceptions.ConnectionClosed:
                        print("Client websocket closed")

                    finally:
                        print("send_to_gemini closed")

                            # =========================
                            # Gemini -> Browser
                            # =========================

            async def receive_from_gemini():
                nonlocal last_voice_transcript
                nonlocal last_detected_product
                nonlocal product_processing
                nonlocal camera_active
                nonlocal camera_session
                nonlocal gemini_alive
                try:
                    print("Receiving from Gemini")
                    while True:
                        try:
                            async for response in session.receive():
                                vad_signal = getattr(response, "voice_activity_detection_signal", None)
                                if vad_signal and getattr(vad_signal, "type", None) == "VOICE_ACTIVITY_STARTED":
                                    print("--> DETECTED USER INTERRUPTION: Sending kill signal to frontend audio worker.")
                                    await client_websocket.send_text(json.dumps({"action": "INTERRUPT"}))
                                server_content = getattr(response, "server_content", None)
                                if getattr(response, "tool_call", None):
                                    for call in response.tool_call.function_calls:
                                        if call.name == "activate_camera":

                                            asyncio.create_task(
                                                handle_tool_call(call)
                                            )

                                        elif call.name == "product_detected":
                                           

                                            description = (call.args or {}).get("description", "").strip()
                                            if (product_processing or description == last_detected_product):
                                                continue
                                            product_processing = True
                                            last_detected_product = description

                                            print(f"[PRODUCT DETECTED] {description}")
                                            await client_websocket.send_text(json.dumps({"action" : "CAMERA_OFF"}))
                                            print("CAMERA OFF MESSAGE SENT")
                                            camera_active = False
                                            camera_session = False

                                            # 1. Send immediate UI update (CRITICAL missing piece)
                                            await client_websocket.send_text(json.dumps({
                                                "user_text": description,
                                                "source": "camera"
                                            }))
                                            

                                            # 2. Run RAG + image fetch
                                            rag_prompt, image_urls = build_rag_prompt(description, website_id=website_id)

                                

                                            if image_urls:

                                                await client_websocket.send_text(json.dumps({
                                                    "image_urls": image_urls, "source": "camera"
                                                }))

                                            

                                            # 3. Inject RAG back into Gemini (for reasoning response)
                                            await session.send_client_content(
                                                turns=types.Content(
                                                    role="user",
                                                    parts=[types.Part.from_text(text=rag_prompt)]
                                                ),
                                                turn_complete=True
                                            )

                                            # 4. Acknowledge tool completion
                                            await session.send_tool_response(function_responses=[
                                                types.FunctionResponse(
                                                    name=call.name,
                                                    id=call.id,
                                                    response={"status": "processed"}
                                                )
                                            ])
                                            product_processing = False
                                        elif call.name == "request_product_images":
                                            if camera_session:
                                                print("[IGNORED] Image request during camera session")
                                                await session.send_tool_response(
                                                    function_responses=[
                                                        types.FunctionResponse(
                                                            name=call.name,
                                                            id = call.id,
                                                            response = {
                                                                "status": "ignored"
                                                            }
                                                        )
                                                        
                                                    ]
                                                )
                                                continue
                                            print(f"[IMAGE REQUEST] {last_user_text}")
                                            
                                            rag_prompt, image_urls = build_rag_prompt(last_user_text, website_id=website_id)
                                            print("imagees found", len(image_urls))
                                            print(image_urls)
                                            if image_urls:
                                                await client_websocket.send_text(json.dumps({"image_urls": image_urls, "source": "text"}))
                                                await session.send_tool_response(
                                                            function_responses=[
                                                                types.FunctionResponse(
                                                                    name=call.name,
                                                                    id=call.id,
                                                                    response={
                                                                        "status": "completed"
                                                                    }
                                                                )
                                                            ]
                                                        )

                                    
                                if server_content is None:
                                    print(f"kakaakakaka")
                                    continue
                                
                                #1. Capture User's Audio Transcription
                                input_tx = getattr(server_content, "input_transcription", None)
                                if input_tx and hasattr(input_tx, "text") and input_tx.text:
                                    user_text = input_tx.text.strip()
                                    if user_text and user_text != last_voice_transcript:
                                        last_voice_transcript = user_text
                                        print('USER TRANSCRIPT:', user_text)
                                        await client_websocket.send_text(json.dumps({"user_text": user_text}))

                                       
                                # 2. Capture Gemini's Audio Transcription (As seen in your logs!)
                                output_tx = getattr(server_content, "output_transcription", None)
                                if output_tx and hasattr(output_tx, "text") and output_tx.text:
                                    print('GEMINI TRANSCRIPT:', output_tx.text)
                                    await client_websocket.send_text(json.dumps({"text": output_tx.text}))

                                # 3. Capture Binary Audio Streams
                                model_turn = getattr(server_content, "model_turn", None)
                                if model_turn and hasattr(model_turn, "parts"):
                                    for part in model_turn.parts:
                                        if getattr(part, "inline_data", None):
                                            audio_b64 = base64.b64encode(part.inline_data.data).decode("utf-8")
                                            await client_websocket.send_text(json.dumps({"audio": audio_b64}))
                                            
                                if getattr(server_content, "turn_complete", False):
                                    print("TURN COMPLETE")
                                    await client_websocket.send_text(json.dumps({"turn_complete": True}))
                                    
                        except Exception as e:
                            print(f"Receive cycle error: {e}")
                            gemini_alive = False
                            camera_active = False
                            camera_session = False
                            try:
                                await client_websocket.send_json({
                                    "type": "session_closed",
                                    "reason": "inactive"
                                })
                            except:
                                pass
                            break
                except websockets.exceptions.ConnectionClosed:
                    print("Client connection closed")
                except Exception as e:
                    print(f"Error receiving from Gemini: {e}")
                finally:
                    print("receive_from_gemini closed")

            send_task = asyncio.create_task(
                send_to_gemini()
            )

            receive_task = asyncio.create_task(
                receive_from_gemini()
            )
            print("Waiting for tasks...")
            done, pending = await asyncio.wait(
                [send_task, receive_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()
            print("Gather returned")
            try:
                await client_websocket.close()
            except:
                pass

    except Exception as e:
        print(
            f"Error in Gemini Session: {e}"
        )

    finally:
        print("Gemini session closed")


# =========================
# SERVER
# =========================

# async def main():

#     async with websockets.serve(
#         gemini_session_handler,
#         "localhost",
#         9080,
#     ):

#         print(
#             "Running websocket server localhost:9080"
#         )

#         await asyncio.Future()


# if __name__ == "__main__":
#     asyncio.run(main())

