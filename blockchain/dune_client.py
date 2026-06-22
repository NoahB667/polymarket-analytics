import os
import time
import logging
from typing import Dict, Any, Generator, Optional
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("dune_client")
logging.basicConfig(level=logging.INFO)

class DuneClient:
    """
    Data Access Layer wrapper around the Dune Analytics API.
    Handles asynchronous execution, state polling, rate-limiting, and chunked pagination.
    """
    def __init__(self):
        self.api_key = os.getenv("DUNE_API_KEY")
        if not self.api_key:
            raise ValueError("Missing critical environment variable: DUNE_API_KEY")
            
        self.base_url = "https://api.dune.com/api/v1"
        self.headers = {
            "X-Dune-API-Key": self.api_key,
            "Content-Type": "application/json"
        }

    def execute_query(self, query_id: int, query_parameters: Optional[Dict[str, Any]] = None) -> str:
        """
        Submits a pre-saved Dune query ID for execution.
        Returns the execution_id needed to track performance status.
        """
        url = f"{self.base_url}/query/{query_id}/execute"
        payload = {"query_parameters": query_parameters or {}}
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            execution_id = data.get("execution_id")
            if not execution_id:
                raise KeyError(f"Execution ID missing from Dune response schema: {data}")
            logger.info(f"Successfully triggered Dune Query {query_id}. Execution ID: {execution_id}")
            return execution_id
        except Exception as e:
            logger.error(f"Failed to execute query {query_id}: {e}")
            raise

    def execute_raw_sql(self, sql_query: str) -> str:
        """
        Submits a raw SQL statement string directly to Dune's SQL execution endpoint.
        Uses the specialized /sql/execute route, matching filarm's data layout.
        """
        url = f"{self.base_url}/sql/execute"
        payload = {
            "sql": sql_query
        }
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=15)
            if response.status_code != 200:
                logger.error(f"Dune API rejected request with status {response.status_code}. Response body: {response.text}")
            response.raise_for_status()
            data = response.json()
            execution_id = data.get("execution_id")
            if not execution_id:
                raise KeyError(f"Execution ID missing from Dune SQL response schema: {data}")
            logger.info(f"Successfully triggered raw Dune SQL block. Execution ID: {execution_id}")
            return execution_id
        except Exception as e:
            logger.error(f"Failed to execute raw SQL on Dune: {e}")
            raise

    def poll_execution_status(self, execution_id: str, max_timeout_secs: int = 1800, poll_interval_secs: int = 10) -> bool:
        """
        Blocks and polls the execution status loop until the query succeeds, fails, or times out.
        Returns True if execution completed successfully.
        """
        url = f"{self.base_url}/execution/{execution_id}/status"
        start_time = time.time()
        
        logger.info(f"Beginning status polling loop for execution: {execution_id}")
        while time.time() - start_time < max_timeout_secs:
            try:
                response = requests.get(url, headers=self.headers, timeout=10)
                response.raise_for_status()
                status_data = response.json()
                
                state = status_data.get("state")
                logger.info(f"Execution {execution_id} current state: {state}")
                
                if state == "QUERY_STATE_COMPLETED":
                    return True
                elif state in ["QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"]:
                    logger.error(f"Dune Engine Error Details: {status_data}")
                    logger.error(f"Dune query execution halted with terminal state: {state}")
                    return False
                    
            except Exception as e:
                logger.warning(f"Transient error while checking status for execution {execution_id}: {e}")
            
            # Back off to respect API rate limits
            time.sleep(poll_interval_secs)
            
        logger.error(f"Dune query execution exceeded maximum timeout of {max_timeout_secs} seconds.")
        return False

    def fetch_results_paginated(self, execution_id: str, limit: int = 25000) -> Generator[Dict[str, Any], None, None]:
        """
        Paginates through millions of rows of data using Dune's offset tokens.
        Yields rows sequentially to minimize memory bloat.
        """
        url = f"{self.base_url}/execution/{execution_id}/results"
        next_offset = None
        has_more = True
        
        while has_more:
            params: Dict[str, Any] = {"limit": limit}
            if next_offset:
                params["offset"] = next_offset
                
            try:
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                
                # Gracefully handle tier rate-limiting (HTTP 429)
                if response.status_code == 429:
                    logger.warning("Dune rate limit hit (429). Backing off for 15 seconds...")
                    time.sleep(15)
                    continue
                    
                response.raise_for_status()
                result_data = response.json()
                
                # Extract matrix metadata structures
                results = result_data.get("result", {})
                rows = results.get("rows", [])
                
                for row in rows:
                    yield row
                    
                # Track pagination parameters safely
                next_offset = result_data.get("next_offset")
                if not next_offset:
                    has_more = False
                    logger.info(f"Finished exhausting all records for execution {execution_id}")
            except Exception as e:
                logger.error(f"Critical error fetching chunk from execution {execution_id}: {e}")
                raise