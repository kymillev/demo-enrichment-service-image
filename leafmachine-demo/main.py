import json
import logging
import os
import uuid
import requests
from typing import Tuple, Any, Dict, List
from kafka import KafkaConsumer, KafkaProducer
import shared

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)


def start_kafka() -> None:
    """
    Start a kafka listener and process the messages by unpacking the image.
    When done it will republish the object, so it can be validated and stored by the processing service.
    :param predictor: The predictor which will be used to run the plant organ segmentation

    """
    consumer = KafkaConsumer(
        os.environ.get("KAFKA_CONSUMER_TOPIC"),
        group_id=os.environ.get("KAFKA_CONSUMER_GROUP"),
        bootstrap_servers=[os.environ.get("KAFKA_CONSUMER_HOST")],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        enable_auto_commit=True,
    )

    producer = KafkaProducer(
        bootstrap_servers=[os.environ.get("KAFKA_PRODUCER_HOST")],
        value_serializer=lambda m: json.dumps(m).encode("utf-8"),
    )

    for msg in consumer:
        logging.info(f"Received message: {str(msg.value)}")
        json_value = msg.value
        try:
            shared.mark_job_as_running(job_id=json_value.get("jobId"))
            digital_object = json_value.get("object")
            image_uri = digital_object.get("ac:accessURI")
            additional_info_annotations, image_height, image_width = run_leafmachine(image_uri)

            # Publish an annotation comment if no plant components were found
            if len(additional_info_annotations) == 0:
                logging.info(f"No results for this herbarium sheet: {image_uri} - jobId: {json_value['jobId']}")
                annotation = map_result_to_empty_annotation(
                    digital_object, image_height=image_height, image_width=image_width
                )

                annotation_event = map_to_annotation_event([annotation], json_value["jobId"])
            # Publish the annotations if plant components were found
            else:
                annotations = map_result_to_annotation(
                    digital_object, additional_info_annotations, image_height=image_height, image_width=image_width
                )
                annotation_event = map_to_annotation_event(annotations, json_value["jobId"])

            logging.info(f"Publishing annotation event: {json.dumps(annotation_event)}")
            publish_annotation_event(annotation_event, producer)

        except Exception as e:
            logging.error(f"Failed to publish annotation event: {e}")
            send_failed_message(json_value["jobId"], str(e), producer)


def map_to_annotation_event(annotations: List[Dict], job_id: str) -> Dict:
    return {"annotations": annotations, "jobId": job_id}


def publish_annotation_event(annotation_event: Dict[str, Any], producer: KafkaProducer) -> None:
    """
    Send the annotation to the Kafka topic.
    :param annotation_event: The formatted list of annotations
    :param producer: The initiated Kafka producer
    :return: Will not return anything
    """
    logging.info(f"Publishing annotation: {str(annotation_event)}")
    producer.send(os.environ.get("KAFKA_PRODUCER_TOPIC"), annotation_event)


def map_result_to_annotation(
    digital_object: Dict,
    additional_info_annotations: List[Dict[str, Any]],
    image_height: int,
    image_width: int,
):
    """
    Given a target object, computes a result and maps the result to an openDS annotation.
    :param digital_object: the target object of the annotation
    :param additional_info_annotations: the result of the computation
    :param image_height: the height of the processed image
    :param image_width: the width of the processed image
    :return: List of annotations
    """
    timestamp = shared.timestamp_now()
    ods_agent = shared.get_agent()
    annotations = list()

    for annotation in additional_info_annotations:
        oa_value = annotation
        oa_selector = shared.build_fragment_selector(annotation, image_width, image_height)
        annotation = shared.map_to_annotation(
            ods_agent,
            timestamp,
            oa_value,
            oa_selector,
            digital_object[shared.ODS_ID],
            digital_object[shared.ODS_TYPE],
            "https://github.com/kymillev/demo-enrichment-service-image",
        )
        annotations.append(annotation)

    return annotations


def map_result_to_empty_annotation(digital_object: Dict, image_height: int, image_width: int):
    """
    Given a target object and no found plant components, map the result to an openDS comment annotation to inform the user.
    :param digital_object: the target object of the annotation

    :return: Annotation event
    """
    timestamp = shared.timestamp_now()
    message = "Leafpriority model found no plant components in this image"
    selector = shared.build_entire_image_fragment_selector(height=image_height, width=image_width)

    annotation = shared.map_to_annotation_str_val(
        ods_agent=shared.get_agent(),
        timestamp=timestamp,
        oa_value=message,
        oa_selector=selector,
        target_id=digital_object[shared.ODS_ID],
        target_type=digital_object[shared.ODS_TYPE],
        dcterms_ref="",
        motivation="oa:commenting",
    )

    return annotation


def run_leafmachine(image_uri: str, model_name: str = "leafpriority") -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Makes an API request to the LeafMachine backend service hosted at IDLab.
    :param image_uri: The URI of the image to be processed
    :param model_name: The name of the model used for inference
    :return: Returns a list of detected plant components, the processed image height, the processed image width
    """
    # Create the payload with image url and model name
    payload = {"image_url": image_uri, "model_name": model_name}

    # Send POST request to the IDLab server
    server_url = "https://herbaria.idlab.ugent.be/inference/process_image/"
    headers = {"Content-Type": "application/json"}
    response = requests.post(server_url, json=payload, headers=headers)

    response.raise_for_status()
    response_json = response.json()

    detections = response_json.get("detections", [])

    img_shape = response_json["metadata"]["orig_img_shape"]
    img_height, img_width = img_shape[:2]

    annotations_list = [
        {"boundingBox": det.get("bbox"), "class": det.get("class_name"), "score": det.get("confidence")}
        for det in detections
    ]

    return annotations_list, img_height, img_width


def send_failed_message(job_id: str, message: str, producer: KafkaProducer) -> None:
    """
    Sends a failure message to the mas failure topic, mas-failed
    :param job_id: The id of the job
    :param message: The exception message
    :param producer: The Kafka producer
    """

    mas_failed = {"jobId": job_id, "errorMessage": message}
    producer.send("mas-failed", mas_failed)


def run_local(example: str) -> None:
    """
    Run the script locally. Can be called by replacing the kafka call with this  a method call in the main method.
    Will call the DiSSCo API to retrieve the specimen data.
    A record ID will be created but can only be used for testing.
    :param example: The full URL of the Digital Specimen to the API (for example
    https://dev.dissco.tech/api/v1/digital-media/TEST/GG9-1WB-N90
    :return: Return nothing but will log the result
    """
    response = requests.get(example)
    json_value = json.loads(response.content).get("data")

    digital_object = json_value.get("attributes")
    image_uri = digital_object.get("ac:accessURI")
    additional_info_annotations, image_height, image_width = run_leafmachine(image_uri)
    # additional_info_annotations = []
    # Publish an annotation comment if no plant components were found
    if len(additional_info_annotations) == 0:
        logging.info(f"No results for this herbarium sheet: {image_uri}")
        annotation = map_result_to_empty_annotation(digital_object, image_height=image_height, image_width=image_width)
        annotation_event = map_to_annotation_event([annotation], str(uuid.uuid4()))
    # Publish the annotations if plant components were found
    else:
        annotations = map_result_to_annotation(
            digital_object, additional_info_annotations, image_height=image_height, image_width=image_width
        )
        annotation_event = map_to_annotation_event(annotations, str(uuid.uuid4()))

    logging.info("Created annotations: " + json.dumps(annotation_event))


if __name__ == "__main__":
    # Local testing
    # specimen_url = "https://sandbox.dissco.tech/api/digital-media/v1/SANDBOX/TC9-7ER-QVP"
    # run_local(specimen_url)
    start_kafka()
